# Deploying to Railway

The app is a single always-on process (`python run.py`) that polls on a timer
and sends Telegram alerts. It has **no inbound HTTP** — on Railway it is a
**worker/service**, not a web service, so it needs no port and no health check.

## 1. Create the service

1. In [Railway](https://railway.com), **New Project → Deploy from GitHub repo**
   and pick `belomarkovicdev/price-tracker`.
2. Railway auto-detects the `Dockerfile` and builds the image. No build config
   needed.

## 2. Set secrets (env vars)

In the service's **Variables** tab, add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token from @BotFather |
| `TELEGRAM_CHAT_ID`   | `@channel` or `-100xxxxxxxxxx` |

These are read from the environment directly (`config.py` uses
`os.environ`), so **no `.env` file is needed in the container**. If Telegram
vars are missing the app still runs and logs alerts in dry-run mode.

## 3. Persist the corpus across redeploys (recommended)

By default the SQLite dbs live inside the container and are **wiped on every
redeploy**, forcing a full re-seed. To keep the accumulated data and medians:

1. Service → add a **Volume**, mount path **`/data`**.
2. Service → **Variables** → add `DB_DIR` = **`/data`**.

The app keeps **one db file per site** in `DB_DIR` (`/data/polovniautomobili.db`,
and `kleinanzeigen.db` if you re-enable it), so redeploys reuse the existing data
instead of re-scraping. Leave `DB_DIR` unset to keep the dbs next to the code
(ephemeral).

> Back-compat: an older `DB_PATH=/data/price_tracker.db` still works — its
> **folder** (`/data`) is used as the volume, and the existing
> `price_tracker.db` is adopted as `polovniautomobili.db` automatically.

## 4. Deploy

Railway builds and starts the container. Watch **Deploy Logs / Observability**
for the poll cycles.

## Notes / caveats

- **First run is heavy, once.** Seeding all searches takes several minutes; with
  a volume it only happens the first time. Without a volume it repeats on every
  redeploy.
- **Datacenter IP.** Scraping from Railway's IP ranges is more likely to be
  blocked than from a home IP. The rate-limit circuit breaker and block
  detection are already in place; if the site pushes back, raise
  `poll_interval_seconds` / `request_delay_seconds` in `config.yaml`.
- **Free tier is short-lived.** Railway's one-time $5 trial credit expires after
  ~30 days; after that this service costs roughly $5/mo (Hobby plan). For a
  permanently-free 24/7 home, an Oracle Cloud Always-Free VM runs this untouched.
