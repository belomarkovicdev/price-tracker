"""Orchestration loop.

Each cycle (every poll_interval_seconds):
  for each enabled site, for each configured search:
    1. scrape one page of listings (polite + circuit-breaker protected)
    2. upsert into the store (accumulates the comparable corpus)
    3. evaluate each listing against its rolling-window bucket
    4. alert on fresh below-average deals via Telegram

Block/circuit-breaker events are handled softly: we log, skip the site for this
cycle, and carry on — never hammer a site that's pushing back.
"""

from __future__ import annotations

import logging
import signal
import time

from .config import Config, load_config
from .evaluator import Evaluator
from .notify import Notification
from .notify.telegram import build_notifier
from .ratelimit import CircuitBreakerTripped
from .scrapers import BlockedError, build_scraper
from .store import Store

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.evaluator = Evaluator(cfg.evaluator)
        self.notifier = build_notifier(cfg.telegram)
        self.scrapers = {}
        for site in cfg.sites:
            if not site.enabled:
                continue
            scraper = build_scraper(site)
            if scraper is not None:
                self.scrapers[site.name] = (site, scraper)
        # Searches we've already seeded (fetched multiple pages for) this run.
        self._seeded: set[str] = set()
        self._running = True

    def stop(self, *_: object) -> None:
        log.info("Shutdown requested; finishing up...")
        self._running = False

    def run_forever(self) -> None:
        log.info(
            "Price-tracker started. %d site(s), poll every %.0fs.",
            len(self.scrapers), self.cfg.poll_interval_seconds,
        )
        while self._running:
            start = time.monotonic()
            try:
                self.run_cycle()
            except Exception:  # never let one bad cycle kill the loop
                log.exception("Unexpected error during cycle")
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, self.cfg.poll_interval_seconds - elapsed)
            self._interruptible_sleep(sleep_for)
        self.store.close()
        log.info("Stopped.")

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def run_cycle(self) -> None:
        for site, scraper in self.scrapers.values():
            for search in site.searches:
                if not self._running:
                    return
                seed_key = f"{site.name}:{search.name}"
                # We always start deeper than the paid "members-first" pages:
                # those inflate the average and don't carry new posts.
                start_page = site.start_page
                if seed_key not in self._seeded:
                    # First sight: seed a wider range to build the corpus so
                    # already-underpriced listings can be judged right away.
                    num_pages = site.seed_pages
                    log.info(
                        "[%s] seeding %r: pages %d-%d (skipping first %d)...",
                        site.name, search.name, start_page,
                        start_page + num_pages - 1, site.skip_pages,
                    )
                else:
                    # Steady state: scan the first few post-skip pages, where
                    # new regular posts appear.
                    num_pages = site.scan_pages
                seeded = self._process_search(
                    scraper, search.name, search.url, start_page, num_pages
                )
                if seeded:
                    self._seeded.add(seed_key)

    def _process_search(
        self, scraper, search_name: str, url: str,
        start_page: int, num_pages: int,
    ) -> bool:
        """Returns True if the fetch succeeded (so it can be marked seeded)."""
        try:
            listings = scraper.fetch_listings(
                search_name, url, start_page, num_pages
            )
        except CircuitBreakerTripped as exc:
            log.error("%s — skipping site this cycle.", exc)
            return False
        except BlockedError as exc:
            log.warning("%s — backing off, skipping search this cycle.", exc)
            return False
        except Exception:
            log.exception("Scrape failed for search %r", search_name)
            return False

        # Pass 1: store everything first, so the comparable corpus is complete
        # before we judge anything. (Judging while still filling the store would
        # evaluate early listings against a near-empty bucket.)
        new_count = deal_count = 0
        touched_buckets: set[str] = set()
        for listing in listings:
            is_new, _prev = self.store.upsert(listing)
            new_count += int(is_new)
            b = listing.bucket()
            if b is not None:
                touched_buckets.add(b)

        # Recompute + persist the market average for every bucket we touched,
        # from the most recent regular listings (paid ads already excluded).
        for b in touched_buckets:
            self.store.update_bucket_stats(
                b, self.cfg.evaluator.window_rows,
                self.cfg.evaluator.bottom_percentile,
            )

        # Pass 2: evaluate every listing against the now-complete window.
        for listing in listings:
            bucket = listing.bucket()
            if bucket is None:
                continue
            comparables = self.store.recent_bucket_prices(
                bucket, self.cfg.evaluator.window_rows, exclude_key=listing.key
            )
            verdict = self.evaluator.evaluate(listing, comparables)
            if not verdict.is_deal:
                continue
            if self.store.already_alerted(listing.key, listing.price):
                continue
            if self.notifier.send(Notification(listing, verdict)):
                self.store.mark_alerted(listing.key, listing.price)
                deal_count += 1
                log.info("DEAL alerted: %s (%s)", listing.title, verdict.reason)

        log.info(
            "[%s] %r: %d listings (%d new), %d deal(s) alerted.",
            scraper.site, search_name, len(listings), new_count, deal_count,
        )
        return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    engine = Engine(cfg)
    signal.signal(signal.SIGINT, engine.stop)
    signal.signal(signal.SIGTERM, engine.stop)
    engine.run_forever()


if __name__ == "__main__":
    main()
