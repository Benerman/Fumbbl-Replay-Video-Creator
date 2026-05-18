# GitHub-Actions deploy automation

This repo has two workflows under `.github/workflows/`:

| Workflow | Trigger | Runner | What it does |
|---|---|---|---|
| `ci.yml` | every push, every PR | GitHub-hosted | Compile-check, import smoke test, docker build. No host access. |
| `deploy.yml` | manual click only | **self-hosted** on your VPS | `git pull` + `docker compose build` + `docker compose up -d` |

The deploy workflow only ever runs when you click "Run workflow" in the Actions tab — there is no `on: push:` trigger on it. Merging to `main` does NOT auto-deploy. That's the primary safety gate.

## One-time host setup

### 1. Clone the repo on the host

```bash
# Put the canonical deploy clone at /opt/fumbbl-replay.
# If you've been deploying from elsewhere, either move it here or
# set a `DEPLOY_PATH` repository variable in GitHub (see step 3).
sudo mkdir -p /opt/fumbbl-replay
sudo chown "$USER:$USER" /opt/fumbbl-replay
git clone https://github.com/Benerman/Fumbbl-Replay-Video-Creator.git /opt/fumbbl-replay
cd /opt/fumbbl-replay
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env  # paste your secrets
```

If you already have a working clone (with `deploy/.env` filled in), just `git pull` it to a clean state — don't re-clone.

### 2. Install the GitHub Actions self-hosted runner

In GitHub: **Settings → Actions → Runners → New self-hosted runner → Linux x64**.

Copy the commands from the page (they include a one-time registration token) and run them in a fresh dir, NOT inside the deploy clone. The runner needs its own workspace:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# paste the download + config commands from the GH UI here
# When `./config.sh` asks for labels, enter:  fumbbl-deploy
# When it asks for a name, anything works (default is the hostname).
# When it asks about working directory, accept the default.
```

Then install it as a systemd service so it survives reboots:

```bash
sudo ./svc.sh install "$USER"
sudo ./svc.sh start
sudo ./svc.sh status
```

The runner now polls GitHub for jobs over outbound HTTPS. No inbound ports are opened. Verify it shows as "Idle" in **Settings → Actions → Runners**.

### 3. Optional: configure the deploy path

If your deploy clone lives somewhere other than `/opt/fumbbl-replay`, set a repository variable:

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

### 5. Optional: add a required-approval gate

If you want a second click of confirmation before the host touches anything, wrap the deploy in a GitHub Environment:

1. **Settings → Environments → New environment** → name it `production`.
2. Check **Required reviewers** and add yourself (or whoever should approve).
3. Edit `.github/workflows/deploy.yml` and uncomment the `environment: production` line.

Now every "Run workflow" click pauses on a "Waiting for approval" screen until a reviewer clicks Approve. (Yes, you can require yourself — it adds a confirm-click but doesn't block solo development.)

## How to deploy

1. Make changes on a feature branch.
2. Open a PR. CI runs automatically on the PR.
3. Wait for CI green → review → merge to `main`.
4. Go to **Actions** tab → **Deploy** workflow → **Run workflow** → ref `main` (or any commit SHA / tag) → **Run workflow**.
5. (If you enabled the environment gate) Approve the deploy on the resulting waiting screen.
6. The self-hosted runner pulls the new code, rebuilds, and restarts the bot + worker. Logs are visible in the Actions run.

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
cd /opt/fumbbl-replay
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

- **"No runner found for labels [self-hosted, fumbbl-deploy]"**: the runner is offline, or has the wrong labels. Check **Settings → Actions → Runners** — the runner should be "Idle". Re-register with `./config.sh remove && ./config.sh --labels fumbbl-deploy` to change labels.
- **"$DEPLOY_PATH is not a git checkout"**: the runner is running as a user without read access to `/opt/fumbbl-replay`, or you set `DEPLOY_PATH` to the wrong location. `ls -la /opt/fumbbl-replay/.git` while su'd to the runner user.
- **Containers don't restart after deploy**: `docker compose up -d` only restarts a container if its image/config changed. If you only changed Python source that's copied at build time, the rebuild + up-d will catch it. If you only changed a mounted volume, no restart is needed.
- **Runner shows "Idle" but workflow stuck on "Queued"**: usually a label mismatch. The workflow says `[self-hosted, fumbbl-deploy]`; the runner must have both labels.
