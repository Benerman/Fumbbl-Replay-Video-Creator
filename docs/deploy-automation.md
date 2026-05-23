# GitHub-Actions deploy automation

This repo has two workflows under `.github/workflows/`:

| Workflow | Trigger | Runner | What it does |
|---|---|---|---|
| `ci.yml` | every push, every PR | GitHub-hosted | Compile-check, import smoke test, docker build. No host access. |
| `deploy.yml` | **manual click only** | **self-hosted** on your VPS | `git pull` + `docker compose build` + `docker compose up -d` |

**Deploy is a manual step.** The deploy workflow only ever runs when you click **Run workflow** in the Actions tab — there is deliberately no `on: push:` or `workflow_run:` trigger. Merging to `main` does **not** deploy. This is the primary control on the self-hosted runner: it executes workflow code directly on the production box, so nothing reaches it without a human explicitly asking.

Branch protection on `main` (step 4 below) still matters — it keeps unvetted code off the ref you'll usually pick to deploy — but the manual click is what actually guards production.

## One-time host setup

### 1. Clone the repo on the host

```bash
# Put the canonical deploy clone at /opt/Fumbbl-Replay-Video-Creator.
# If you've been deploying from elsewhere, either move it here or
# set a `DEPLOY_PATH` repository variable in GitHub (see step 3).
sudo mkdir -p /opt/Fumbbl-Replay-Video-Creator
sudo chown "$USER:$USER" /opt/Fumbbl-Replay-Video-Creator
git clone https://github.com/Benerman/Fumbbl-Replay-Video-Creator.git /opt/Fumbbl-Replay-Video-Creator
cd /opt/Fumbbl-Replay-Video-Creator
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env  # paste your secrets
```

If you already have a working clone (with `deploy/.env` filled in), just `git pull` it to a clean state — don't re-clone.

### 2. Install the GitHub Actions self-hosted runner

#### Scripted (recommended)

`deploy/runner-setup.sh` does the whole dance — download the runner, register it against this repo with the `fumbbl-deploy` label, and install it as a systemd service. It's the runner equivalent of `deploy/deploy.sh`: one idempotent script you can re-run any time.

Run it **on the VPS, as the dedicated unprivileged runner user** (not root):

```bash
# Option A — let the gh CLI mint the registration token (needs `gh auth login`):
cd /opt/Fumbbl-Replay-Video-Creator
./deploy/runner-setup.sh

# Option B — pass a one-time registration token yourself. Get it from
# GitHub → Settings → Actions → Runners → New self-hosted runner:
RUNNER_TOKEN=AABBCC... ./deploy/runner-setup.sh
```

It auto-detects arch (x64 / arm64), resolves the latest runner version, verifies the download checksum, registers with `--labels fumbbl-deploy`, and installs + starts the systemd service. Re-running re-registers cleanly (`--replace`). The script also warns if the runner user can't reach the Docker daemon — the deploy workflow runs `docker compose` as this user, so it must be in the `docker` group.

To tear the runner down later (deregisters from GitHub + removes the service):

```bash
./deploy/runner-setup.sh --uninstall
```

Useful env overrides: `RUNNER_REPO_URL`, `RUNNER_LABELS`, `RUNNER_NAME`, `RUNNER_DIR`, `RUNNER_VERSION` — see the header of `deploy/runner-setup.sh`.

#### Manual (fallback)

If you'd rather do it by hand, follow GitHub's own instructions at **Settings → Actions → Runners → New self-hosted runner → Linux x64**. Run the commands in a fresh dir, NOT inside the deploy clone — the runner needs its own workspace:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# paste the download + config commands from the GH UI here
# When `./config.sh` asks for labels, enter:  fumbbl-deploy
# When it asks for a name, anything works (default is the hostname).
# When it asks about working directory, accept the default.
sudo ./svc.sh install "$USER"
sudo ./svc.sh start
sudo ./svc.sh status
```

Either way, the runner now polls GitHub for jobs over outbound HTTPS. No inbound ports are opened. Verify it shows as "Idle" in **Settings → Actions → Runners**.

### 3. Optional: configure the deploy path

If your deploy clone lives somewhere other than `/opt/Fumbbl-Replay-Video-Creator`, set a repository variable:

**Settings → Secrets and variables → Actions → Variables → New repository variable**

| Name | Value |
|---|---|
| `DEPLOY_PATH` | `/path/to/your/clone` |

Variables are visible in workflow logs; that's fine — they're not secrets.

### 4. Branch-protect `main`

The deploy workflow defaults to deploying `main`. Make sure `main` only contains reviewed code:

**Settings → Branches → Add branch protection rule**:
- Branch name pattern: `main`
- ✅ Require a pull request before merging
- ✅ Require approvals (1 is fine for solo projects — it stops accidental direct pushes from the web UI / a misconfigured local)
- ✅ Require status checks to pass before merging
  - Add `CI / Python syntax + imports` and `CI / Docker image builds` so PRs can't merge with red CI
- ✅ Do not allow bypassing the above settings
- ❌ Allow force pushes (off)
- ❌ Allow deletions (off)

### 5. Optional: add a second required-approval gate

Deploy already requires a manual click. If you want an *additional* approval step on top of that — useful when more than one person can trigger deploys, or to force a deliberate confirm — wrap the deploy in a GitHub Environment:

1. **Settings → Environments → New environment** → name it `production`.
2. Check **Required reviewers** and add yourself (or whoever should approve).
3. Edit `.github/workflows/deploy.yml` and uncomment the `environment: production` line.

Now each "Run workflow" click pauses on a "Waiting for approval" screen until a reviewer clicks Approve. (Yes, you can require yourself — it adds a confirm-click but doesn't block solo development.)

## How to deploy

Deploy is always an explicit manual action:

1. Make changes on a feature branch.
2. Open a PR. CI runs automatically on the PR.
3. Get CI green → review → merge to `main`.
4. When you're ready to ship, go to **Actions** tab → **Deploy** → **Run workflow** → ref `main` (or any branch / tag / SHA) → **Run workflow**.
5. (If you enabled the environment gate) Approve the deploy on the resulting waiting screen.
6. The self-hosted runner pulls that ref, rebuilds, and restarts the bot + worker. Logs are visible in the Actions run.

## How to roll back

Same workflow, different ref:

1. **Actions** → **Deploy** → **Run workflow**.
2. In the **ref** input, enter the short SHA of the last-known-good commit (find it in `git log` or the previous successful deploy run).
3. Run.

The deploy script does `git checkout <sha>` so the host ends up on the older code. Re-running with `main` later moves it forward again.

## How to deploy without CI/CD (the manual escape hatch)

`deploy/deploy.sh` is also runnable directly on the host:

```bash
ssh you@your-vps
cd /opt/Fumbbl-Replay-Video-Creator
./deploy/deploy.sh                # deploys main
./deploy/deploy.sh v1.1           # deploys a feature branch
./deploy/deploy.sh a58faea        # deploys a specific commit
```

Same script the workflow calls. Useful if the runner is offline or for emergency rollbacks.

## Security notes

- **The runner can execute arbitrary code from workflow files.** Anyone with write access to `.github/workflows/` on `main` can run code on your VPS. Branch protection + PR review on `main` is what stops that.
- **PRs from forks do NOT use self-hosted runners** — that's GitHub's default for public repos and you should leave it off. (Settings → Actions → General → Fork pull request workflows from outside collaborators → "Require approval for all outside collaborators".)
- **The runner has filesystem access** to whatever the runner-user can read. Run it as a dedicated unprivileged user (not root), and don't put it in the same shell session as anything privileged.
- **Secrets in workflows**: this setup doesn't need any. No SSH keys, no Docker registry tokens. Outbound HTTPS to GitHub is the only required network connectivity.

## Troubleshooting

- **"No runner found for labels [self-hosted, fumbbl-deploy]"**: the runner is offline, or has the wrong labels. Check **Settings → Actions → Runners** — the runner should be "Idle". Re-register cleanly by re-running `./deploy/runner-setup.sh` (it uses `--replace`), or set `RUNNER_LABELS=...` first to change labels.
- **"$DEPLOY_PATH is not a git checkout"**: the runner is running as a user without read access to `/opt/Fumbbl-Replay-Video-Creator`, or you set `DEPLOY_PATH` to the wrong location. `ls -la /opt/Fumbbl-Replay-Video-Creator/.git` while su'd to the runner user.
- **Containers don't restart after deploy**: `docker compose up -d` only restarts a container if its image/config changed. If you only changed Python source that's copied at build time, the rebuild + up-d will catch it. If you only changed a mounted volume, no restart is needed.
- **Runner shows "Idle" but workflow stuck on "Queued"**: usually a label mismatch. The workflow says `[self-hosted, fumbbl-deploy]`; the runner must have both labels.
