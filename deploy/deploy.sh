#!/usr/bin/env bash
# deploy/deploy.sh — restart the running stack with the latest code.
#
# Single source of truth for what a deploy does. Called by:
#   - the Deploy workflow on the self-hosted runner (.github/workflows/deploy.yml)
#   - you, manually on the host, when you don't want to use the UI
#
# Usage:
#   ./deploy/deploy.sh             # deploys origin/main
#   ./deploy/deploy.sh v1.1        # deploys a specific branch
#   ./deploy/deploy.sh a58faea     # deploys a specific commit SHA
#
# Idempotent. Safe to run twice in a row.

set -euo pipefail

REF="${1:-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Preflight: the user running this must be able to write the git checkout.
# The #1 self-hosted-deploy failure is a clone created by root while the
# runner runs as an unprivileged user — `git fetch` then dies with
# "cannot open '.git/FETCH_HEAD': Permission denied". Fail early with the
# exact fix instead of a cryptic git error.
if ! ( : > .git/.deploy_write_test ) 2>/dev/null; then
    echo "ERROR: $(whoami) cannot write to $REPO_ROOT/.git"
    echo "The deploy clone isn't owned by the user running this deploy."
    echo "Fix on the host (swap in the runner user if it differs):"
    echo "    sudo chown -R $(whoami):$(whoami) $REPO_ROOT"
    exit 1
fi
rm -f .git/.deploy_write_test

echo "==> [$(date -u +%H:%M:%SZ)] Fetching from origin..."
git fetch --all --tags --prune

echo "==> Checking out $REF..."
# `git checkout` works for branches, tags, and SHAs. For branches we
# also fast-forward to origin in case the local branch is stale.
git checkout "$REF"
if git symbolic-ref -q HEAD >/dev/null; then
    # On a branch (not detached HEAD): fast-forward to origin.
    git pull --ff-only "origin" "$REF" || {
        echo "ERROR: cannot fast-forward — local has diverged from origin."
        echo "Resolve manually or pass a commit SHA to deploy a specific point."
        exit 1
    }
fi

DEPLOYED_SHA="$(git rev-parse --short HEAD)"
DEPLOYED_MSG="$(git log -1 --pretty=format:'%s' HEAD)"
echo "==> Deploying $DEPLOYED_SHA: $DEPLOYED_MSG"

cd deploy

echo "==> Building images..."
# --pull refreshes the base-image layers so OS/security updates land too.
# No-op for any service that has no build: section.
docker compose build --pull

echo "==> Pulling any prebuilt (non-built) images..."
# Covers compose files whose services use a prebuilt `image:` instead of a
# `build:` section — without this, `up -d` would keep the stale image.
# Harmless no-op (errors swallowed) for build-from-source services.
docker compose pull --quiet 2>/dev/null || true

echo "==> Stopping the current stack for a clean restart..."
# Full down (not just `up -d`) so we always start from fresh containers
# off the image we just built — `up -d` alone can skip recreating a
# service whose compose config didn't change even though its image did.
docker compose down --remove-orphans

echo "==> Starting a fresh stack..."
docker compose up -d

echo "==> Status:"
docker compose ps

echo "==> Recent logs (last 30 lines):"
docker compose logs --tail=30 --timestamps || true

echo "==> [$(date -u +%H:%M:%SZ)] Deploy complete: $DEPLOYED_SHA"
