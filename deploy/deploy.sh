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
docker compose build

echo "==> Restarting services..."
docker compose up -d

echo "==> Status:"
docker compose ps

echo "==> Recent logs (last 30 lines):"
docker compose logs --tail=30 --timestamps || true

echo "==> [$(date -u +%H:%M:%SZ)] Deploy complete: $DEPLOYED_SHA"
