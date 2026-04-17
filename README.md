# Stockholm Running Clubs — Weekly Newsletter Sync

Pulls upcoming events and posts from every Stockholm running club you follow and drops each entry as a new row in a Google Sheet. Runs every Monday morning via GitHub Actions so your weekly newsletter has fresh data waiting in the sheet.

## What it does

- **Strava**: Uses your personal Strava account (you're already a member of the clubs) to auto-discover every club and fetch their upcoming group events.
- **Instagram**: Parses RSS.app feeds — one per running club Instagram account — so you don't need any Meta Graph API access.
- **Google Sheet**: Appends new rows, deduped on the post/event URL. Safe to re-run anytime.

## Sheet columns

`source | club | title | date | location | description | link | image_url | engagement | fetched_at`

- `source` is `strava` or `instagram`
- `engagement` is the joined-athletes count for Strava; blank for Instagram (RSS doesn't expose likes)

---

## One-time setup

### 1. Create the Google Sheet

1. Create a new Google Sheet (any name — e.g. "Running clubs newsletter feed").

2. Copy its ID from the URL: `docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`. You'll paste that into a GitHub secret later.
3. Leave the first tab named `Events` (or change `worksheet_name` in `config.yaml`).

### 2. Create a Google service account

This is what the script authenticates as when writing to the Sheet.

1. Go to https://console.cloud.google.com/ and create a project (or reuse one).
2. Enable the **Google Sheets API** for that project (APIs & Services → Library → Sheets API → Enable).
3. IAM & Admin → Service Accounts → **Create service account**. Give it a name like `running-clubs-sync`. No roles needed.
4. On the service account, go to **Keys** → **Add key → JSON**. Download the file. This is your `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Open the JSON, find `"client_email"`, copy that email address.
6. Go back to your Google Sheet → Share → paste the service account email → give it **Editor** access. This is what lets the script write rows.

### 3. Create a Strava API application

1. Go to https://www.strava.com/settings/api and click **Create an App**.
2. Fill in the form. For "Authorization Callback Domain" use `localhost`.
3. After creating, note the **Client ID** and **Client Secret**.

### 4. Get your Strava refresh token (one-time)

You authorize your own Strava account once to give the script long-lived read access.

1. In your browser, visit this URL (replace `YOUR_CLIENT_ID`):
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=read,read_all
   ```
2. Approve access. Strava redirects you to `http://localhost/?state=&code=XXXXXXXX&scope=...` — the page won't load but that's fine. Copy the `code` value from the URL bar.
3. Exchange that code for tokens. Replace the three values and run:
   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d code=THE_CODE_FROM_STEP_2 \
     -d grant_type=authorization_code
   ```
4. Copy the `refresh_token` from the response. That's your `STRAVA_REFRESH_TOKEN` — it doesn't expire unless you revoke it in Strava settings.

### 5. Set up RSS.app feeds for Instagram

1. Sign up at https://rss.app/.
2. Use the Instagram RSS generator: https://rss.app/rss-feed/instagram-rss-feed
3. For each Stockholm running club Instagram account, paste the URL (e.g. `https://www.instagram.com/stockholmrunners/`) and click **Generate**. Copy the resulting feed URL (ends in `.xml`).
4. Paste one feed URL per line into `config.yaml` under `instagram_feeds:`:
   ```yaml
   instagram_feeds:
     - https://rss.app/feeds/xxxxxxxx.xml   # stockholmrunners
     - https://rss.app/feeds/yyyyyyyy.xml   # midnattslöparna
   ```

### 6. Push to GitHub and add secrets

1. Create a new GitHub repo (private is fine) and push this folder.
2. In the repo → Settings → Secrets and variables → Actions → **New repository secret**. Add each of:
   - `STRAVA_CLIENT_ID`
   - `STRAVA_CLIENT_SECRET`
   - `STRAVA_REFRESH_TOKEN`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the **entire JSON file contents** as the value
   - `GOOGLE_SHEET_ID`
3. The workflow in `.github/workflows/weekly-sync.yml` runs every Monday at 06:00 UTC. You can also run it manually from the Actions tab (**Run workflow** button).

---

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in .env with the same values you used for GitHub secrets,
# then export them:
set -a; source .env; set +a

python -m src.main
```

The first run writes the header row and appends all upcoming events + recent posts. Subsequent runs only append rows with new `link` values.

---

## Customising

- **Schedule**: edit the cron line in `.github/workflows/weekly-sync.yml`.
- **Columns**: edit `HEADERS` in `src/sheets.py` and the corresponding dataclasses in `src/strava.py` / `src/rss.py`.
- **Filter by club**: if you want to exclude some Strava clubs from the fetch, add a `strava_exclude_clubs:` list to `config.yaml` and filter on `club["id"]` inside `strava.fetch_all_events`.

---

## Known limitations

- **Instagram engagement**: RSS.app's Instagram feeds don't include like/comment counts, so the `engagement` column is blank for Instagram rows. If you need this, the only reliable path is Meta's Graph API with the club granting your app access — much heavier lift.
- **RSS.app free tier**: limited to a handful of feeds. Upgrade if you're tracking more than ~5 clubs.
- **Strava rate limits**: 200 requests per 15 minutes, 2000 per day. Plenty for a weekly sync across dozens of clubs.
- **Private Strava clubs**: only events from clubs you're a member of are returned. Make sure you've joined each one from the account whose refresh token you're using.
