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

By default the SQLite db lives inside the container and is **wiped on every
redeploy**, forcing a full re-seed (with the current config that's 13 searches ×
`seed_pages` ≈ 100+ page fetches). To keep the accumulated corpus and medians:

1. Service → **Variables** → add a **Volume**, mount path **`/data`**.
2. Service → **Variables** → add `DB_PATH` = **`/data/price_tracker.db`**.

The app reads `DB_PATH` and stores the db on the volume (creating `/data` if
needed), so redeploys reuse the existing corpus instead of re-scraping. Leave
`DB_PATH` unset to keep the db next to the code (ephemeral).

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
