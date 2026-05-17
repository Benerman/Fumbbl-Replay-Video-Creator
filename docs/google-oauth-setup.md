# Google OAuth setup — `client_secret.json`

The worker uploads videos to YouTube using your Google account's
permission. Google grants that permission through OAuth 2.0, which
requires two things on our end:

1. A **client secrets file** (`client_secret.json`) downloaded from
   the Google Cloud Console once when you set up the bot.
2. A **refresh token** the bot mints later by running through the
   OAuth flow against that client. The refresh token gets encrypted
   and stored in `data/app.sqlite3`.

This doc walks through step 1 (download the secrets). Step 2 is
covered in [`docs/bring-up-checklist.md`](bring-up-checklist.md).

## TL;DR

You're creating a **Desktop / Installed-app OAuth 2.0 Client ID**
under a project that has the **YouTube Data API v3** enabled. The
download from that page is `client_secret.json`. Drop it in
`./data/client_secret.json` and you're done with this step.

## Step-by-step

### 1. Create (or pick) a Google Cloud project

1. Go to <https://console.cloud.google.com/>. Sign in with the
   Google account that owns the YouTube channel you want to upload
   to (or another account that has Manager permission on the brand
   channel).
2. Top-left, click the project dropdown → **New Project**.
3. Name it something obvious like `fumbbl-highlights`. The
   organization can stay "No organization" for personal use.
4. Click **Create**, then switch to that project once it's ready.

### 2. Enable the YouTube Data API v3

1. From the left nav: **APIs & Services** → **Library**.
2. Search for **YouTube Data API v3**.
3. Click the result, then **Enable**. (You can disable it any time
   if you stop running the bot.)

### 3. Configure the OAuth consent screen

You only need to do this once per project. Google rolled out a new
console layout in late 2024 — the steps depend on which one you see.

**Quick way to tell which layout you're on:** open **APIs &
Services → OAuth consent screen**. If you see a row of **tabs**
near the top (`Branding`, `Audience`, `Clients`, `Data Access`,
`Verification center`), you're on the **new layout** — follow §3a.
If you see a single long form with numbered steps (`App
information` / `Scopes` / `Test users` / `Summary`), you're on the
**classic layout** — follow §3b.

#### 3a. New layout (post-2024)

Each setting lives in its own tab. Go through them in order; you
can come back any time.

1. **Get started / Branding** tab
   - **App name**: `FUMBBL Highlights Bot` (any human name).
   - **User support email**: your email.
   - **Developer contact email**: your email.
   - Logo / app domain / links: leave blank.
   - Click **Save**.

2. **Audience** tab
   - **User type**: **External**. (Internal only appears if you
     have a Google Workspace org; pick External for personal use.)
   - **Publishing status**: leave as **Testing** — that's fine
     indefinitely as long as you stay under 100 test users.
   - **Test users** section: click **+ Add users** and add every
     Google account that will authorize the bot — your own, plus
     any guild admins who will run `/highlight-config set-youtube`.
     Up to 100.
   - Click **Save**.

3. **Data Access** tab  ← *this is where the scopes live now*
   - Click **Add or remove scopes**.
   - In the picker, search for `youtube` and check:
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.readonly`
   - Click **Update**, then **Save** on the Data Access page.

4. **Clients** tab → continue to step 4 below to create the
   OAuth Client ID.

#### 3b. Classic layout (pre-2024)

If you see the old wizard:

1. Left nav: **APIs & Services** → **OAuth consent screen**.
2. **User Type**: External. Click **Create**.
3. **App information**:
   - **App name**: `FUMBBL Highlights Bot`
   - **User support email**: your email
   - **Developer contact email**: your email
   - Logo, app domain, links: leave blank.
4. **Scopes**: click **Add or remove scopes**, search for
   `youtube`, check:
   - `.../auth/youtube.upload`
   - `.../auth/youtube.readonly`
   Save and continue.
5. **Test users**: add every Google account that will authorize
   the bot. Up to 100.
6. **Summary**: review and click **Back to dashboard**.

#### Either way

You don't need to publish the app. Testing mode works indefinitely
as long as you stay under 100 test users. During the OAuth flow,
test users will see a "This app isn't verified" screen — click
**Advanced → Go to FUMBBL Highlights Bot (unsafe)** to proceed.
Non-test-users see a hard block (this is the intended security
boundary).

### 4. Create the OAuth Client ID

1. Left nav: **APIs & Services** → **Credentials**.
2. **+ CREATE CREDENTIALS** → **OAuth client ID**.
3. **Application type**: **Desktop app**. (This is the right type
   for our setup — it lets the bot run a local callback server on
   `localhost:38080/oauth/callback` without needing a publicly
   reachable redirect URI. The bootstrap script uses
   `localhost:38081` for the one-time default-channel flow.)
4. **Name**: `fumbbl-highlights-bot` (anything; you'll see this on
   the consent screen).
5. Click **Create**.
6. A dialog pops up — click **DOWNLOAD JSON**. The file is named
   something like `client_secret_…apps.googleusercontent.com.json`.
7. Rename it to `client_secret.json` and drop it into the repo's
   `data/` directory:

```bash
mkdir -p data
mv ~/Downloads/client_secret_*.json data/client_secret.json
```

> **The file contains your OAuth client ID + client secret.** It's
> NOT a user credential — anyone with the file can show your bot's
> name on the consent screen, but they can't access any YouTube
> account without going through your test-users list. Even so,
> `data/` is in `.gitignore` for a reason; don't commit it.

### 5. Verify

The bring-up checklist's first sanity check is:

```bash
test -f data/client_secret.json && echo OK
```

If that prints `OK` you're done with the Google side. Move on to
[`docs/bring-up-checklist.md`](bring-up-checklist.md).

## Troubleshooting

**"This app hasn't been verified by Google"** during authorization
→ Expected for Testing-mode apps. Click **Advanced → Go to FUMBBL
Highlights Bot (unsafe)**. Only your test users (added in step 3.5)
can reach this screen; others see a hard block.

**"Access blocked: <project> has not completed the Google
verification process"** → The Google account trying to authorize
isn't on the test-users list. Add them in
**OAuth consent screen → Test users**.

**No refresh_token returned during bootstrap** → Google only mints
a refresh token on the **first** consent for a given user+client.
If you've authorized before, revoke the bot at
<https://myaccount.google.com/permissions> and re-run the bootstrap.

**Multiple Google accounts in the same browser** → The consent
screen always picks the most-recently-active account. Run the
bootstrap in an incognito window so you can pick deliberately.

**Quota** → Default YouTube API quota is 10,000 units/day. A single
video upload costs ~1,600 units, so ~6 uploads/day per project. If
you'll exceed that, request more quota at
<https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas>.
