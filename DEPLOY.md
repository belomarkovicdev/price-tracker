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

## 3. Persist the medians across redeploys (recommended)

The db is small now — it holds only the **medians** (`model_prices`) and the
alert-dedup log, not individual listings (those live in memory). Without a
volume it's wiped on every redeploy, so the last-known medians are lost and the
tracker starts querying from scratch until the in-memory buffer refills. To keep
the medians and the alert log:

1. Service → add a **Volume**, mount path **`/data`**.
2. Service → **Variables** → add `DB_DIR` = **`/data`**.

The app keeps **one db file per site** in `DB_DIR` (`/data/polovniautomobili.db`,
and `kleinanzeigen.db` if you re-enable it). Leave `DB_DIR` unset to keep the dbs
next to the code (ephemeral).

> Note: the per-listing sample is **in memory**, so every restart/redeploy
> re-seeds to refill it regardless of the volume — expect a scraping burst and a
> short warm-up before medians are fully current. The volume only preserves the
> aggregate medians + alert log.
>
> Back-compat: an older `DB_PATH=/data/price_tracker.db` still works — its
> **folder** (`/data`) is used as the volume, and the existing
> `price_tracker.db` is adopted as `polovniautomobili.db` automatically (its old
> per-listing tables are dropped and the file vacuumed).

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
