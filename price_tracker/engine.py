"""Orchestration loop.

Each cycle (every poll_interval_seconds):
  for each enabled site, for each configured search:
    1. scrape one page of listings (polite + circuit-breaker protected)
    2. upsert into the store (accumulates the comparable corpus)
    3. evaluate each listing against the stored per-(site, brand, model, year,
       fuel) price stats
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
from .maintenance import refresh_medians
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
        # Searches we've already seeded (fetched multiple pages for). Loaded
        # from the db so a restart trusts the existing corpus and skips the
        # wide re-seed — it only does the light steady-state scan for new posts.
        self._seeded: set[str] = self.store.seeded_keys()
        if self._seeded:
            log.info("Resuming: %d search(es) already seeded in db.",
                     len(self._seeded))
        # First periodic median refresh fires one interval from startup (not on
        # every restart), so restarts don't spam the Telegram heartbeat.
        self._last_median_refresh = time.time()
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
            self._maybe_refresh_medians()
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, self.cfg.poll_interval_seconds - elapsed)
            self._interruptible_sleep(sleep_for)
        self.store.close()
        log.info("Stopped.")

    def _maybe_refresh_medians(self) -> None:
        """Once per `median_refresh_interval_seconds`, force-recompute the stored
        medians for every group seen in that window and post a Telegram heartbeat.
        This owns all periodic refreshing; the per-scan step only seeds a row for
        a group the first time it's seen. Guarded so a failure here never kills
        the poll loop."""
        interval = self.cfg.median_refresh_interval_seconds
        if interval <= 0:
            return
        if time.time() - self._last_median_refresh < interval:
            return
        self._last_median_refresh = time.time()
        try:
            refresh_medians(
                self.store, self.notifier,
                self.cfg.evaluator.bottom_percentile,
                since_seconds=interval,
            )
        except Exception:
            log.exception("Periodic median refresh failed")

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
                was_seeded = seed_key in self._seeded
                if site.seed_enabled and not was_seeded:
                    # First sight ever (across runs): seed a wider range to build
                    # the corpus so already-underpriced listings can be judged
                    # right away.
                    num_pages = site.seed_pages
                    log.info(
                        "[%s] seeding %r: pages %d-%d (skipping first %d)...",
                        site.name, search.name, start_page,
                        start_page + num_pages - 1, site.skip_pages,
                    )
                else:
                    # Steady state (or a seed-disabled site that only ever
                    # watches for new posts): scan the first few post-skip pages,
                    # where new regular posts appear.
                    num_pages = site.scan_pages
                ok = self._process_search(
                    scraper, site.name, search.name, search.url,
                    start_page, num_pages,
                )
                # Record the seed only on the first successful wide fetch, and
                # persist it so future restarts skip re-seeding this search.
                if site.seed_enabled and ok and not was_seeded:
                    self._seeded.add(seed_key)
                    self.store.mark_seeded(seed_key)

    def _process_search(
        self, scraper, site_name: str, search_name: str, url: str,
        start_page: int, num_pages: int,
    ) -> bool:
        """Returns True if the fetch succeeded (so it can be marked seeded)."""
        try:
            listings = scraper.fetch_listings(
                search_name, url, start_page, num_pages,
                stored_attrs=lambda lid: self.store.get_listing_attrs(
                    f"{site_name}:{lid}"),
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

        # Pass 1: store everything first, so the corpus is complete before we
        # judge anything. (Judging while still filling the store would evaluate
        # early listings against a near-empty group.)
        new_count = deal_count = 0
        touched: set[tuple[str, str, int, str]] = set()
        for listing in listings:
            is_new, _prev = self.store.upsert(listing)
            new_count += int(is_new)
            if listing.brand and listing.model and listing.year and listing.fuel:
                touched.add(
                    (listing.brand, listing.model, listing.year, listing.fuel))

        # Seed a price-stats row for any newly-seen group that doesn't have one
        # yet, so its listings can be judged in this same cycle. Existing rows
        # are left untouched here and kept current by the hourly median refresh
        # (see _maybe_refresh_medians), so a scan never repeats the full-sample
        # recompute. Scoped to this site so markets are never mixed; one row per
        # group, upserted — never duplicated.
        for brand, model, year, fuel in touched:
            if self.store.get_model_price(
                    site_name, brand, model, year, fuel) is None:
                self.store.update_model_price(
                    site_name, brand, model, year, fuel,
                    bottom_percentile=self.cfg.evaluator.bottom_percentile,
                )

        # Pass 2: evaluate each listing against the single stored average price
        # for its site+brand+model+year+fuel group.
        for listing in listings:
            if not (listing.brand and listing.model and listing.year
                    and listing.fuel):
                continue
            row = self.store.get_model_price(
                site_name, listing.brand, listing.model, listing.year,
                listing.fuel,
            )
            if row is None:
                continue
            verdict = self.evaluator.evaluate(listing, row)
            if not verdict.is_deal:
                continue
            if self.store.already_alerted(listing.key, listing.price):
                continue
            if self.notifier.send(Notification(listing, verdict)):
                self.store.mark_alerted(listing.key, listing.price)
                deal_count += 1
                log.info("DEAL alerted: %s (%s)", listing.title, verdict.reason)

        # One commit for the whole scan: all the upserts + stats updates above
        # batch into a single fsync instead of one per listing.
        self.store.commit()

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
