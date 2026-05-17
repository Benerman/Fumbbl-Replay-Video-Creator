# Bring-up checklist

Go through these in order. Each box describes what to do, why,
and how to verify before moving on. Total time the first time:
about 30 minutes (most of it is waiting on Discord developer
portal + Google verification).

---

## 0. Prereqs on the host

- [ ] **Docker + Docker Compose v2 installed.** Verify:
      ```bash
      docker --version
      docker compose version
      ```
      Both should print versions, not errors.
- [ ] **Port `38080` reachable from your browser.** This is where
      the bot's OAuth callback listens during per-guild setup.
      If you're running on a VPS, open the port in the firewall;
      if local, no action needed.
- [ ] **About 2 GB of free disk** (the docker image is ~1 GB, plus
      `data/` and `jobs/` grow over time).

---

## 1. Get the Google `client_secret.json`

- [ ] **Follow [`docs/google-oauth-setup.md`](google-oauth-setup.md).**
      That walks you through creating a Cloud project, enabling the
      YouTube Data API, configuring the OAuth consent screen with
      the two YouTube scopes, and downloading the **Desktop**
      OAuth client.
- [ ] **Drop the file at `data/client_secret.json`:**
      ```bash
      mkdir -p data
      mv ~/Downloads/client_secret_*.json data/client_secret.json
      test -f data/client_secret.json && echo OK
      ```
- [ ] **Add yourself + any guild admins as test users** under
      OAuth consent screen → Test users. Otherwise they'll see
      "Access blocked" on the consent screen.

---

## 2. Create the Discord application + bot

- [ ] Go to <https://discord.com/developers/applications> and
      click **New Application**. Name it something like
      `fumbbl-highlights`.
- [ ] In **Bot** → **Reset Token**, copy the bot token (one-time
      reveal). Save it for step 3.
- [ ] In **Bot** → **Privileged Gateway Intents**, you don't need
      any of the privileged intents — leave them all off.
- [ ] In **OAuth2** → **URL Generator**, check:
      - Scopes: `bot`, `applications.commands`
      - Bot Permissions: `Send Messages`, `Embed Links`,
        `Use Slash Commands`, `Read Message History`,
        `View Channels`
- [ ] Copy the generated invite URL, open it in your browser, and
      invite the bot to your Discord server. **Don't run any
      slash commands yet** — the bot isn't running.

---

## 3. Configure `deploy/.env`

- [ ] Copy the example:
      ```bash
      cp deploy/.env.example deploy/.env
      ```
- [ ] **Generate a Fernet master key.** Pick whichever of these
      works in your shell — they all produce the same kind of
      32-byte urlsafe-base64 token Fernet expects:

      ```bash
      # Option A — pure stdlib Python (no project deps needed):
      python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'

      # Option B — openssl (no Python needed):
      openssl rand -base64 32 | tr '+/' '-_'

      # Option C — once the docker image is built, generate inside it:
      cd deploy && docker compose run --rm bot \
          python -c 'from services.common.crypto import generate_master_key; print(generate_master_key())'
      ```

      Paste the output into `FERNET_MASTER_KEY` in `deploy/.env`.
      **Once you save the file, do not change this value** —
      rotating it invalidates every stored YouTube refresh token.
- [ ] Paste the **bot token** from step 2 into `DISCORD_BOT_TOKEN`.
- [ ] Find your **Application ID** at
      <https://discord.com/developers/applications> → your app →
      General Information → Application ID. Paste into
      `DISCORD_APPLICATION_ID`.
- [ ] Leave the other defaults (paths point at `/app/...`
      inside the container which is where the volumes mount).

Sanity check:
```bash
grep -E '^(DISCORD_BOT_TOKEN|DISCORD_APPLICATION_ID|FERNET_MASTER_KEY)=' deploy/.env \
  | awk -F= 'length($2) < 10 { print "TOO SHORT:", $1; bad=1 } END { exit bad }' && echo OK
```

---

## 4. Build and start the stack

- [ ] **Build the image:**
      ```bash
      cd deploy
      docker compose build
      ```
      First build is slow (downloads Python deps + ffmpeg);
      subsequent rebuilds are cached.
- [ ] **Start both services:**
      ```bash
      docker compose up -d
      docker compose ps
      ```
      Both `bot` and `worker` should be `running`.
- [ ] **Tail logs to confirm:**
      ```bash
      docker compose logs -f bot
      ```
      Expect:
      ```
      services.bot: opened sqlite at /app/data/app.sqlite3
      services.bot: logged in as fumbbl-highlights#XXXX (id=...)
      services.bot: oauth callback listening on http://0.0.0.0:38080/oauth/callback
      ```
      ` Ctrl-C` to detach (containers keep running).

---

## 5. Mint the default-channel refresh token

The bot needs at least one set of YouTube credentials before any
upload can succeed. This step minted an **encrypted** refresh token
for the bot operator's YouTube channel — the default that gets used
unless a guild admin runs `/highlight-config set-youtube`.

- [ ] **Run the bootstrap inside the worker container:**
      ```bash
      docker compose exec worker python -m services.worker.youtube_upload --bootstrap-default
      ```
- [ ] Follow the printed URL in a browser **on the same machine**
      (it binds to `OAUTH_CALLBACK_HOST=127.0.0.1` by default). If
      you're SSH'd into a remote host, either:
      - Forward the port: `ssh -L 38081:127.0.0.1:38081 user@host`
        then run the bootstrap in the SSH session; OR
      - Set `OAUTH_CALLBACK_HOST=0.0.0.0` in `deploy/.env` and
        open the printed URL on your remote host (less secure;
        revert after bootstrap).
- [ ] Sign in with the **brand account** for the YouTube channel
      where you want videos uploaded. Click through "Access
      blocked → Advanced → Go to (unsafe)" if you see it.
- [ ] You should see `Saved default credentials. Channel id: UC...`
      in the worker's log. The encrypted refresh token is now in
      `data/app.sqlite3.bot_defaults`.

Sanity check:
```bash
docker compose exec worker python -c '
from services.common import db
row = db.get_bot_defaults()
print("default channel id:", row["yt_channel_id"] if row else "MISSING")'
```

---

## 6. Smoke-test the bot in Discord

- [ ] In your Discord server (the one you invited the bot to),
      type `/generate-highlight 4700552` in any channel.
- [ ] Expected flow (timestamps approximate):
      - `< 1 s`: bot acknowledges with **"📋 Queued. Rendering
        will start shortly…"**
      - `~ 3 s`: edits to **"🎬 Rendering the highlight reel…"**
      - `~ 90 s`: edits to **"☁️ Uploading to YouTube…"**
      - `~ 3 min`: posts the YouTube link in the same channel.
- [ ] Verify the video plays on YouTube (it'll be **public** by
      default).
- [ ] Re-run the same command. It should reply instantly with the
      cached link instead of re-rendering (dedup hit).

---

## 7. (Optional) Per-guild YouTube override

If you want a specific server to upload to a YouTube channel that
**isn't** the bot operator's default:

- [ ] In Discord, run **as a server admin** (Manage Server perm):
      `/highlight-config set-youtube`
- [ ] The bot DMs you a Google auth URL (valid 10 minutes). Open
      it in a browser **on the same network as the bot host** so
      the redirect to `http://<host>:38080/oauth/callback` reaches
      the bot.
- [ ] Sign in as the brand account for the target YouTube channel.
- [ ] The bot replies "configured ✅" in the channel. Subsequent
      `/generate-highlight` calls from that guild will upload to
      its own channel.
- [ ] Revert any time with `/highlight-config reset`.

---

## Failure mode triage

**Bot starts then exits immediately** → Most likely a bad
`DISCORD_BOT_TOKEN` or `FERNET_MASTER_KEY`. Check `docker compose
logs bot` for the traceback.

**`/generate-highlight` replies "queued" but never updates** →
The worker isn't running, or it's not picking up jobs. Check
`docker compose logs worker`. If you see "claimed job ..."
followed by an exception, the render or upload failed — look at
the message in the resulting `jobs/done/<uuid>.json`.

**`/generate-highlight` replies "No default YouTube credentials
configured"** → You skipped step 5. Run the bootstrap.

**Render succeeds but upload fails with "quotaExceeded"** → You've
hit the default 10,000-units/day YouTube API quota (a single
upload costs ~1,600 units). Either wait until midnight Pacific
time or request more quota at
<https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas>.

**`out/` directory not getting cleaned up** → Cleanup only runs
on success. Inspect `jobs/done/*.json` for any with
`status: "error"` and resolve those first.

---

## Daily ops

- **Logs**: `docker compose logs -f bot` / `docker compose logs -f worker`
- **Restart**: `docker compose restart bot` (or `worker`)
- **Update code**: `git pull && docker compose build && docker compose up -d`
- **Backup**: copy `data/app.sqlite3` somewhere safe. Losing it
  means losing all guild overrides + the dedup table (the bot
  will re-upload replays it had already processed). The
  `services/worker/youtube_reconcile.py` script can backfill the
  dedup table from `Match {id}` titles on the configured channel
  if you ever need to recover.
