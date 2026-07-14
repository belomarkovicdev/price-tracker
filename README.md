# price-tracker

Watches classified sites, learns the market price per like-for-like group, and
sends a **Telegram alert when a listing is meaningfully below average** — so you
can flip it at the normal price.

Ships two sites — **polovniautomobili.com** and **kleinanzeigen.de** — behind a
pluggable interface, so more slot in without touching the rest of the pipeline.
Prices are compared **within a site and within a fuel type** only: a German and
a Serbian market don't compare, and neither do a diesel and a petrol of the same
model-year (diesel sits higher, so mixing them would flag every petrol as a
"deal"). See *Per-site, per-fuel comparison* below.

## How it works

Every `poll_interval_seconds` (default 20s), for each configured search:

1. **Scrape** one page of listings — politely, and behind a hard safety limit.
2. **Buffer** them in memory — a rolling window of recent comparables, deduped
   by ad id (see *In-memory buffer* below). Nothing per-listing is written to
   disk.
3. **Evaluate** each listing against its group's **median + MAD + low-percentile**,
   computed live from the buffer for every `(site, brand, model, year, fuel)`
   group with more than 4 comparables.
4. **Alert** on Telegram for fresh listings that are clearly below market —
   with a scam/typo guard so absurdly-cheap junk is ignored.

Once an hour those medians are written to SQLite (`model_prices`) as the durable,
queryable output — see *In-memory buffer*.

### Per-site, per-fuel comparison

The comparison group key is `(site, brand, model, year, fuel)`. `site` keeps
markets separate (Kleinanzeigen ≠ polovniautomobili); `fuel` keeps diesel and
petrol of the same model-year in separate pools, because diesel is consistently
dearer — lumping them together would make every petrol car look cheap against a
diesel-inflated median. A listing is only judged once its own group has at least
`min_samples` comparables.

### In-memory buffer

Individual listings are **never written to disk** — they live in an in-memory
rolling buffer (one per site), deduped by ad id so a car re-seen every cycle
counts once. Once an hour the engine:

1. **prunes** the buffer to `retention_window_seconds` (default 24h) — anything
   not seen within the window ages out;
2. **rebuilds** the `model_prices` table from what's left: one median/MAD/
   low-percentile row per group with **more than 4** comparables (delete-then-
   insert, so no stale rows survive); and
3. posts a **Telegram heartbeat** (`🔄 Updating price database…` → `✅ … updated`).

So the median reflects the recent market, and the **db holds only the aggregate**
(`model_prices`) plus the alert-dedup log — a few KB, not a growing archive.
Cadence is `median_refresh_interval_seconds` (default `3600`; `0` disables it);
the window is `retention_window_seconds` (default `86400`).

Trade-off: the buffer is **process-local and volatile.** On restart it starts
empty and refills by re-seeding, so there's a warm-up before medians are current
again (the previous run's `model_prices` stays queryable meanwhile). There is no
standalone maintenance command — only the running tracker holds the buffer.

### One database per site

Each site gets its **own SQLite file inside a shared volume** —
`polovniautomobili.db`, and later `kleinanzeigen.db`, side by side in the folder
set by `DB_DIR` (or the folder of a legacy `DB_PATH`; default: next to the code).
Adding a site never touches another's data, and each db is kept single-site (rows
from any other site are purged on open). The old single `price_tracker.db` is
adopted as `polovniautomobili.db` automatically on first start, and its retired
per-listing tables are dropped and the file vacuumed.

### Why the design is the way it is

- **Buffer in memory, persist only the median per `(site, brand, model, year, fuel)`:**
  the median is robust but needs its whole sample to compute — so we keep the
  recent sample in RAM and write just the one aggregate row per group. The db
  stays tiny and bounded; nothing per-listing is ever written. The cost is a
  warm-up after restart while the buffer refills — an acceptable trade for a
  DB that can't balloon.
- **Median/MAD, not mean:** one overpriced or one scam listing can't skew it.
- **SQLite (WAL) for the aggregate only:** standard-library (no extra
  dependency). It stores just `model_prices` (the medians) and the alert-dedup
  log — both survive restarts, so the last-known medians stay queryable and we
  never re-alert the same ad. The per-listing sample is deliberately *not*
  persisted; it's rebuilt in memory from the next seed.
- **Hardcoded circuit breaker:** never more than 60 requests/min to a site, in
  any rolling 60s window. It is deliberately **not** a config knob — it's a bug
  backstop. At the 20s cadence normal traffic is ~3 req/min, so it should never
  trip; if it does, the site is skipped and it backs off.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Create a **`.env`** file (this is the file the app reads; it's git-ignored so
your secrets stay out of version control):

```
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_CHAT_ID=<your channel id>
```

**Getting the channel id:** create the bot with [@BotFather](https://t.me/BotFather),
add it to your channel as an **admin**, post any message in the channel, then
open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read the
`channel_post.chat.id` (a negative `-100…` number). Public channels can also use
`@channelusername` as the id.

If Telegram isn't configured, the app still runs and logs alerts (dry-run) so you
can test.

## Configure what to watch

Edit `config.yaml`. Each search is just a normal search-results URL for the site
— build one in your browser with the filters you want and paste it in:

```yaml
sites:
  polovniautomobili:
    searches:
      - name: "VW Golf 7 diesel manual"
        url: "https://www.polovniautomobili.com/auto-oglasi/pretraga?brand=140&model%5B%5D=10613&fuel%5B%5D=3400"
  kleinanzeigen:
    seed_enabled: false   # don't bulk-import history; just watch for new posts
    searches:
      - name: "Aichach ≤2000 EZ 2009+"
        url: "https://www.kleinanzeigen.de/s-autos/aichach/preis:2000:/c216l7190r100+autos.ez_i:2009%2C"
```

> **Tip:** narrow, specific searches (one brand+model) fill each group fast and
> give meaningful stats quickly. A broad "all cars" search spreads listings
> across many thin groups, so it takes far longer before it can judge anything —
> and that's by design (it won't alert on thin data, i.e. fewer than
> `min_samples` listings for that group).

### Site notes

- **kleinanzeigen** runs with `seed_enabled: false`: it never bulk-imports back
  pages, it just watches page 1 (sorted newest-first) for new posts and grows
  the corpus organically. Its results cards don't carry the structured fields
  (Marke/Modell/**Kraftstoffart**/Erstzulassung), so those come from each ad's
  detail page — fetched **once per ad, only the first time it's seen**. Ads
  already in the DB reuse their stored details, so no page is re-fetched.

## Run

```bash
python run.py
```

Leave it running (24/7). It polls every 20s, stays polite, and self-heals if a
site pushes back. Stop with Ctrl-C. On restart the last-known medians are still
in the db, but the in-memory sample starts empty, so it re-seeds to refill the
buffer before medians are fully current again.

To run it in the cloud instead of on your machine, see **[DEPLOY.md](DEPLOY.md)**
(ships a `Dockerfile`; steps for Railway).

## Adding another site later

1. Create `price_tracker/scrapers/<site>.py` with a `@register("<site>")`
   `Scraper` subclass implementing `fetch_listings()`.
2. Import it in `price_tracker/scrapers/__init__.py`.
3. Add a `sites.<site>` block in `config.yaml`.

The store, evaluator, and Telegram notifier are site-agnostic and need no
changes. If a site's list page lacks structured fields (as Kleinanzeigen does),
have `fetch_listings()` use its `stored_attrs` argument to skip re-fetching a
per-ad detail page for an ad still in the in-memory buffer — see
`scrapers/kleinanzeigen.py`.

## Project layout

```
run.py                     entry point
config.yaml                poll cadence, evaluator knobs, sites & searches
.env                       secrets (Telegram)
Dockerfile                 container image (see DEPLOY.md)
DEPLOY.md                  cloud deploy steps (Railway)
price_tracker/
  config.py                load config + .env
  models.py                Listing + like-for-like bucket key
  ratelimit.py             RateLimiter + HARDCODED CircuitBreaker
  buffer.py                in-memory rolling window of recent listings (per site)
  store.py                 SQLite (per site): model_prices medians + alert log
  maintenance.py           prune buffer + rebuild medians (called hourly)
  evaluator.py             median/MAD deal detection from per-group stats
  engine.py                orchestration loop
  scrapers/                base + registry + polovni + kleinanzeigen
  notify/                  base + telegram (+ dry-run log)
```
