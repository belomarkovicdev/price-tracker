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
2. **Store** them in SQLite, accumulating a corpus of comparables over time.
3. **Evaluate** each listing against the **stored price stats for its
   `(site, brand, model, year, fuel)`** — a single row holding the **median +
   MAD + low-percentile** of every comparable seen. Those stats are recomputed
   from the full sample at most **once per 24h**; each scan just reads that row.
4. **Alert** on Telegram for fresh listings that are clearly below market —
   with a scam/typo guard so absurdly-cheap junk is ignored.

### Per-site, per-fuel comparison

The comparison group key is `(site, brand, model, year, fuel)`. `site` keeps
markets separate (Kleinanzeigen ≠ polovniautomobili); `fuel` keeps diesel and
petrol of the same model-year in separate pools, because diesel is consistently
dearer — lumping them together would make every petrol car look cheap against a
diesel-inflated median. A listing is only judged once its own group has at least
`min_samples` comparables.

### Why the design is the way it is

- **Stored stats per `(site, brand, model, year, fuel)`, refreshed once/24h:** the median is
  more robust than a mean but needs the whole sample to compute — so we compute
  it during a once-a-day refresh (which already reads the full sample) and store
  it in a single row. Every scan then reads just that one row (O(1) per listing)
  instead of re-scanning many comparables. Best of both: robust *and* cheap.
- **Median/MAD, not mean:** one overpriced or one scam listing can't skew it.
- **SQLite (WAL, batched commits):** standard-library (no extra dependency), and
  everything persists — the comparable corpus, the per-model-year stats, and
  which searches have already been seeded. So a **restart resumes instantly**
  from the stored corpus instead of re-scraping to rebuild it. Writes run in WAL
  mode and commit once per scan, so a cycle costs one disk flush, not one per ad.
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
site pushes back. Stop with Ctrl-C. On restart it resumes from the SQLite corpus
— it won't re-scrape pages to rebuild data it already has.

To run it in the cloud instead of on your machine, see **[DEPLOY.md](DEPLOY.md)**
(ships a `Dockerfile`; steps for Railway).

## Adding another site later

1. Create `price_tracker/scrapers/<site>.py` with a `@register("<site>")`
   `Scraper` subclass implementing `fetch_listings()`.
2. Import it in `price_tracker/scrapers/__init__.py`.
3. Add a `sites.<site>` block in `config.yaml`.

The store, evaluator, and Telegram notifier are site-agnostic and need no
changes. If a site's list page lacks structured fields (as Kleinanzeigen does),
have `fetch_listings()` use its `stored_attrs` argument to fetch a per-ad detail
page only for ads not already in the DB — see `scrapers/kleinanzeigen.py`.

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
  store.py                 SQLite: listings, price history, alerts,
                           per-(site,model,year,fuel) stats, seed state
  evaluator.py             median/MAD deal detection from stored per-group stats
  engine.py                orchestration loop
  scrapers/                base + registry + polovni + kleinanzeigen
  notify/                  base + telegram (+ dry-run log)
```
