#!/usr/bin/env bash
# deploy/runner-setup.sh — provision a GitHub Actions self-hosted runner
# on THIS host so the Deploy workflow (.github/workflows/deploy.yml) can
# pick up jobs labelled `self-hosted, fumbbl-deploy`.
#
# This is the scripted equivalent of the copy-paste flow in
# docs/deploy-automation.md → "Install the GitHub Actions self-hosted
# runner". It downloads the runner, registers it against the repo with
# the right label, and installs it as a systemd service that survives
# reboots. It is the single source of truth for runner provisioning,
# the same way deploy/deploy.sh is for deploys.
#
# Run this ON THE VPS, as the dedicated UNPRIVILEGED runner user (NOT
# root). sudo is used only to install/manage the systemd service.
#
# Quick start:
#   # Get a short-lived registration token from:
#   #   GitHub → repo → Settings → Actions → Runners → New self-hosted runner
#   # then:
#   RUNNER_TOKEN=AABBCC... ./deploy/runner-setup.sh
#
#   # ...or, if the `gh` CLI is installed AND authenticated, the script
#   # mints the registration token for you — no token needed:
#   ./deploy/runner-setup.sh
#
#   # Tear it down (deregisters from GitHub + removes the service):
#   ./deploy/runner-setup.sh --uninstall
#
# Env overrides (all optional):
#   RUNNER_TOKEN     registration token (or removal token with --uninstall).
#                    Omit to have `gh` mint one.
#   RUNNER_REPO_URL  default: https://github.com/Benerman/Fumbbl-Replay-Video-Creator
#   RUNNER_LABELS    default: fumbbl-deploy   (MUST match deploy.yml's runs-on)
#   RUNNER_NAME      default: $(hostname)
#   RUNNER_DIR       default: $HOME/actions-runner   (its own dir — NOT the deploy clone)
#   RUNNER_VERSION   default: latest (resolved from the GitHub API)
#
# Idempotent: re-running re-registers with --replace and reinstalls the
# service cleanly, so it's safe to run again after a label change or to
# refresh an expired registration.

set -euo pipefail

# ---- config (env-overridable) ---------------------------------------------
REPO_URL="${RUNNER_REPO_URL:-https://github.com/Benerman/Fumbbl-Replay-Video-Creator}"
LABELS="${RUNNER_LABELS:-fumbbl-deploy}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner}"
RUNNER_VERSION="${RUNNER_VERSION:-latest}"
TOKEN="${RUNNER_TOKEN:-}"
MODE="install"

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) MODE="uninstall" ;;
        --token) TOKEN="$2"; shift ;;
        --token=*) TOKEN="${1#*=}" ;;
        -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

# owner/repo for the GitHub API (token minting / removal).
REPO_SLUG="${REPO_URL#https://github.com/}"
REPO_SLUG="${REPO_SLUG%.git}"

log() { echo "==> [$(date -u +%H:%M:%SZ)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---- preflight ------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    die "Do not run as root. Create a dedicated unprivileged user and run as them
       (the runner executes workflow code; root would hand it the whole box)."
fi
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v tar  >/dev/null 2>&1 || die "tar is required."
command -v sudo >/dev/null 2>&1 || die "sudo is required (for the systemd service)."

# Mint a token via the gh CLI when one wasn't supplied.
#   $1 = "registration" (install) | "remove" (uninstall)
mint_token_via_gh() {
    local kind="$1" endpoint
    command -v gh >/dev/null 2>&1 || return 1
    gh auth status >/dev/null 2>&1 || return 1
    endpoint="repos/${REPO_SLUG}/actions/runners/${kind}-token"
    log "Minting ${kind} token via gh for ${REPO_SLUG}..."
    gh api --method POST "$endpoint" -q .token 2>/dev/null
}

# ---- uninstall ------------------------------------------------------------
if [ "$MODE" = "uninstall" ]; then
    [ -x "$RUNNER_DIR/svc.sh" ] || die "No runner found at $RUNNER_DIR (nothing to uninstall)."
    cd "$RUNNER_DIR"
    log "Stopping + removing systemd service..."
    sudo ./svc.sh stop 2>/dev/null || true
    sudo ./svc.sh uninstall 2>/dev/null || true
    if [ -z "$TOKEN" ]; then
        TOKEN="$(mint_token_via_gh remove || true)"
    fi
    if [ -n "$TOKEN" ]; then
        log "Deregistering runner from GitHub..."
        ./config.sh remove --token "$TOKEN" || true
    else
        echo "No removal token (set RUNNER_TOKEN or authenticate gh) — the runner" >&2
        echo "will still appear in GitHub as 'Offline'. Remove it from the UI:" >&2
        echo "  Settings → Actions → Runners → ${RUNNER_NAME} → Remove." >&2
    fi
    log "Uninstalled. Runner files remain at $RUNNER_DIR (delete manually if desired)."
    exit 0
fi

# ---- resolve arch + version ----------------------------------------------
case "$(uname -m)" in
    x86_64|amd64) ARCH="x64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    armv7l) ARCH="arm" ;;
    *) die "Unsupported architecture: $(uname -m)" ;;
esac

if [ "$RUNNER_VERSION" = "latest" ]; then
    command -v python3 >/dev/null 2>&1 \
        || die "python3 needed to resolve the latest runner version. Either install it
       or pin a version, e.g. RUNNER_VERSION=2.319.1 ./deploy/runner-setup.sh"
    log "Resolving latest runner version from the GitHub API..."
    RUNNER_VERSION="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')"
    [ -n "$RUNNER_VERSION" ] || die "Could not determine latest runner version."
fi
log "Runner version: $RUNNER_VERSION ($ARCH)"

# ---- download (skip if already extracted) ---------------------------------
mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
if [ ! -x "./config.sh" ]; then
    PKG="actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz"
    URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${PKG}"
    log "Downloading $URL ..."
    curl -fsSL -o "$PKG" "$URL"

    # Best-effort checksum verification using the release asset digest.
    if command -v python3 >/dev/null 2>&1 && command -v sha256sum >/dev/null 2>&1; then
        EXPECTED="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/tags/v${RUNNER_VERSION} \
            | python3 -c '
import json,sys
rel=json.load(sys.stdin)
want=sys.argv[1]
for a in rel.get("assets",[]):
    if a.get("name")==want:
        d=a.get("digest") or ""
        print(d.split(":",1)[1] if d.startswith("sha256:") else "")
        break
' "$PKG" 2>/dev/null || true)"
        if [ -n "$EXPECTED" ]; then
            echo "${EXPECTED}  ${PKG}" | sha256sum -c - \
                || die "Checksum mismatch for $PKG — refusing to extract."
            log "Checksum verified."
        else
            echo "Note: no published digest found; skipping checksum verification." >&2
        fi
    fi

    log "Extracting..."
    tar xzf "$PKG"
    rm -f "$PKG"
else
    log "Runner already extracted in $RUNNER_DIR — reusing."
fi

# ---- register -------------------------------------------------------------
if [ -z "$TOKEN" ]; then
    TOKEN="$(mint_token_via_gh registration || true)"
fi
[ -n "$TOKEN" ] || die "No registration token. Provide one:
       RUNNER_TOKEN=... ./deploy/runner-setup.sh
   Get it from: GitHub → repo → Settings → Actions → Runners → New self-hosted
   runner (Linux). Or install + 'gh auth login' to have this script mint one."

log "Registering runner '${RUNNER_NAME}' with labels '${LABELS}'..."
./config.sh --unattended --replace \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$LABELS" \
    --work _work

# ---- install as a systemd service -----------------------------------------
log "Installing systemd service (survives reboots)..."
sudo ./svc.sh uninstall 2>/dev/null || true   # clean any prior install
sudo ./svc.sh install "$(whoami)"
sudo ./svc.sh start
sudo ./svc.sh status || true

# ---- docker sanity note ---------------------------------------------------
# The Deploy workflow runs `docker compose` AS this runner user, so it must
# be able to talk to the Docker daemon without sudo.
if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
        echo
        echo "WARNING: '$(whoami)' can't reach the Docker daemon. The deploy" >&2
        echo "workflow runs 'docker compose' as this user and will fail. Fix with:" >&2
        echo "    sudo usermod -aG docker $(whoami)" >&2
        echo "then restart the runner service so it picks up the new group:" >&2
        echo "    cd $RUNNER_DIR && sudo ./svc.sh stop && sudo ./svc.sh start" >&2
    fi
else
    echo
    echo "WARNING: docker not found on PATH. Install Docker + the compose plugin" >&2
    echo "before deploying (the workflow runs 'docker compose build/up')." >&2
fi

echo
log "Done. The runner should now show as 'Idle' in:"
echo "      ${REPO_URL}/settings/actions/runners"
echo "    Trigger a deploy from the Actions tab → Deploy → Run workflow."
