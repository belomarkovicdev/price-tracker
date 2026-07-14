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

from .buffer import ListingBuffer
from .config import Config, load_config
from .evaluator import Evaluator
from .maintenance import refresh_medians
from .notify import Notification
from .notify.telegram import build_notifier
from .ratelimit import CircuitBreakerTripped
from .scrapers import BlockedError, build_scraper
from .store import Store, open_site_store, price_stats

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.evaluator = Evaluator(cfg.evaluator)
        self.notifier = build_notifier(cfg.telegram)
        self.scrapers: dict[str, tuple] = {}
        self.stores: dict[str, Store] = {}
        # Per-site in-memory rolling buffer of recent listings — the sample the
        # medians are computed from. Nothing per-listing is written to disk.
        self.buffers: dict[str, ListingBuffer] = {}
        # One db per enabled site, inside the shared volume (cfg.db_dir). Each
        # store purges any other site's rows on open, so the files stay
        # single-site even after adopting the old shared price_tracker.db.
        for site in cfg.sites:
            if not site.enabled:
                continue
            scraper = build_scraper(site)
            if scraper is None:
                continue
            self.scrapers[site.name] = (site, scraper)
            self.stores[site.name] = open_site_store(cfg.db_dir, site.name)
            self.buffers[site.name] = ListingBuffer()
        # Searches seeded this run, per site. The buffer is volatile, so we seed
        # on every start to refill it — this set is in-memory, not persisted.
        self._seeded: dict[str, set[str]] = {name: set() for name in self.stores}
        # First periodic refresh fires one interval from startup (not on every
        # restart), so restarts don't spam the Telegram heartbeat.
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
        for store in self.stores.values():
            store.close()
        log.info("Stopped.")

    def _maybe_refresh_medians(self) -> None:
        """Once per `median_refresh_interval_seconds`, prune each site's db to the
        retention window and recompute its medians, posting one Telegram
        heartbeat around the whole batch. This owns all periodic refreshing; the
        per-scan step only seeds a row the first time a group is seen. Guarded so
        a failure here never kills the poll loop."""
        interval = self.cfg.median_refresh_interval_seconds
        if interval <= 0:
            return
        if time.time() - self._last_median_refresh < interval:
            return
        self._last_median_refresh = time.time()
        try:
            hours = self.cfg.retention_window_seconds / 3600.0
            self.notifier.send_text(
                f"\U0001F504 Updating price database — pruning to the last "
                f"{hours:.0f}h and recomputing market medians…")
            tot_g = tot_l = tot_p = 0
            for name, store in self.stores.items():
                g, listings, pruned = refresh_medians(
                    store, self.buffers[name], self.notifier,
                    self.cfg.evaluator.bottom_percentile,
                    window_seconds=self.cfg.retention_window_seconds,
                    announce=False, min_rows=self.cfg.median_min_samples,
                )
                tot_g += g
                tot_l += listings
                tot_p += pruned
            self.notifier.send_text(
                f"✅ Price database updated: {tot_g} market group(s) from "
                f"{tot_l} listing(s) (last {hours:.0f}h); pruned {tot_p} old.")
        except Exception:
            log.exception("Periodic median refresh failed")

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def run_cycle(self) -> None:
        for name, (site, scraper) in self.scrapers.items():
            store = self.stores[name]
            buffer = self.buffers[name]
            seeded = self._seeded[name]
            for search in site.searches:
                if not self._running:
                    return
                seed_key = f"{site.name}:{search.name}"
                # We always start deeper than the paid "members-first" pages:
                # those inflate the average and don't carry new posts.
                start_page = site.start_page
                was_seeded = seed_key in seeded
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
                    store, buffer, scraper, site.name, search.name, search.url,
                    start_page, num_pages,
                )
                # Mark seeded for this run so we don't re-seed it again. Not
                # persisted: the buffer is volatile, so a restart re-seeds.
                if site.seed_enabled and ok and not was_seeded:
                    seeded.add(seed_key)

    def _process_search(
        self, store: Store, buffer: ListingBuffer, scraper, site_name: str,
        search_name: str, url: str, start_page: int, num_pages: int,
    ) -> bool:
        """Returns True if the fetch succeeded (so it can be marked seeded)."""
        try:
            listings = scraper.fetch_listings(
                search_name, url, start_page, num_pages,
                stored_attrs=lambda lid: buffer.attrs(f"{site_name}:{lid}"),
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

        # Add this page's listings to the in-memory buffer (deduped by key).
        # Nothing is written to disk here — medians are rebuilt from the buffer
        # on the hourly refresh, and only alerts touch the db.
        now = time.time()
        before = len(buffer)
        for listing in listings:
            buffer.upsert(listing, now)
        new_count = max(0, len(buffer) - before)

        # Group medians, computed live from the buffer for every group with
        # enough comparables to judge against (evaluator.min_samples). Note this
        # is the *alerting* threshold; persisting a median to the db uses the
        # separate, higher median_min_samples (see _maybe_refresh_medians).
        bp = self.cfg.evaluator.bottom_percentile
        min_n = self.cfg.evaluator.min_samples
        stats_by_group = {
            group: price_stats(prices, bp)
            for group, prices in buffer.group_prices().items()
            if len(prices) >= min_n
        }

        # Evaluate each listing against its group's median; alert on fresh deals.
        deal_count = 0
        for listing in listings:
            if not (listing.brand and listing.model and listing.year
                    and listing.fuel):
                continue
            stats = stats_by_group.get(
                (listing.brand, listing.model, listing.year, listing.fuel))
            if stats is None:
                continue
            verdict = self.evaluator.evaluate(listing, stats)
            if not verdict.is_deal:
                continue
            if store.already_alerted(listing.key, listing.price):
                continue
            if self.notifier.send(Notification(listing, verdict)):
                store.mark_alerted(listing.key, listing.price)
                deal_count += 1
                log.info("DEAL alerted: %s (%s)", listing.title, verdict.reason)

        log.info(
            "[%s] %r: %d listings (%d new in buffer), %d deal(s) alerted.",
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
