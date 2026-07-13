# price-tracker

Watches classified sites, learns the market price per like-for-like group, and
sends a **Telegram alert when a listing is meaningfully below average** — so you
can flip it at the normal price.

Currently ships one site (**polovniautomobili.com**), behind a pluggable
interface so more sites (Kleinanzeigen, etc.) slot in later without touching the
rest of the pipeline.

## How it works

Every `poll_interval_seconds` (default 20s), for each configured search:

1. **Scrape** one page of listings — politely, and behind a hard safety limit.
2. **Store** them in SQLite, accumulating a corpus of comparables over time.
3. **Evaluate** each listing against the **rolling window of the 50 most recent
   comparables in its like-for-like bucket** (brand + model + 2-year band +
   25k-km band + fuel + gearbox), using **median + MAD** (robust to outliers).
4. **Alert** on Telegram for fresh listings that are clearly below market —
   with a scam/typo guard so absurdly-cheap junk is ignored.

### Why the design is the way it is

- **Rolling window of last 50 (per bucket):** the average self-refreshes and
  reflects the current market; old rows fall out automatically.
- **Median/MAD, not mean:** one overpriced or one scam listing can't skew it.
- **SQLite:** standard-library (no extra dependency), survives restarts, and the
  comparable corpus grows while we still fetch just one light page per cycle.
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

Edit `config.yaml`. Each search is just a normal polovniautomobili search-results
URL — build one in your browser with the filters you want and paste it in:

```yaml
sites:
  polovniautomobili:
    searches:
      - name: "VW Golf 7 diesel manual"
        url: "https://www.polovniautomobili.com/auto-oglasi/pretraga?brand=140&model%5B%5D=10613&fuel%5B%5D=3400"
```

> **Tip:** narrow, specific searches (one brand+model) fill buckets fast and give
> meaningful averages quickly. A broad "all cars" search spreads listings across
> many thin buckets, so it takes far longer before it can judge anything — and
> that's by design (it won't alert on thin data).

## Run

```bash
python run.py
```

Leave it running (24/7). It polls every 20s, stays polite, and self-heals if a
site pushes back. Stop with Ctrl-C.

## Adding another site later

1. Create `price_tracker/scrapers/<site>.py` with a `@register("<site>")`
   `Scraper` subclass implementing `fetch_listings()`.
2. Import it in `price_tracker/scrapers/__init__.py`.
3. Add a `sites.<site>` block in `config.yaml`.

The store, evaluator, and Telegram notifier are site-agnostic and need no
changes. (Note: harder sites like Kleinanzeigen will need a real browser
(Playwright) + German residential proxies in their scraper — the base class's
rate-limit / circuit-breaker / block-detection plumbing is already there for it.)

## Project layout

```
run.py                     entry point
config.yaml                poll cadence, evaluator knobs, sites & searches
.env                       secrets (Telegram)
price_tracker/
  config.py                load config + .env
  models.py                Listing + like-for-like bucket key
  ratelimit.py             RateLimiter + HARDCODED CircuitBreaker
  store.py                 SQLite: listings, price history, alerts
  evaluator.py             median/MAD rolling-window deal detection
  engine.py                orchestration loop
  scrapers/                base + registry + polovni
  notify/                  base + telegram (+ dry-run log)
```
