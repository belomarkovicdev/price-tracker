"""Rolling-window DB maintenance: prune old listings, recompute the stored
medians, and announce it on Telegram.

The db is a rolling window, not an archive: every hour we drop listings not seen
within the retention window (default 24h) and recompute, for each
(site, brand, model, year, fuel) group with more than 4 comparables left, a
single stored median/MAD/low-percentile row. So the file stays bounded to about
one window of data, and the median reflects the recent market rather than the
whole history.

It runs two ways, sharing `refresh_medians()`:
  * from inside the engine loop, every `median_refresh_interval_seconds`; and
  * standalone via `python -m price_tracker.maintenance` — a one-off run, or
    driven by an external scheduler (Windows Task Scheduler / cron).
"""

from __future__ import annotations

import logging
import time

from .config import load_config
from .notify import Notifier
from .notify.telegram import build_notifier
from .store import Store, open_site_store

log = logging.getLogger(__name__)

# Only (re)compute a group's median once it has MORE THAN 4 comparables in the
# window — a median over a handful of listings isn't meaningful. `min_rows` is
# the minimum row count to update, so "more than 4" is 5.
_MIN_ROWS_TO_UPDATE = 5


def refresh_medians(
    store: Store,
    notifier: Notifier,
    bottom_percentile: float,
    window_seconds: float = 24 * 3600.0,
    announce: bool = True,
    min_rows: int = _MIN_ROWS_TO_UPDATE,
) -> tuple[int, int, int]:
    """Prune listings older than the rolling window, then recompute the stored
    median for every group with at least `min_rows` comparables left. If
    `announce`, bracket it with Telegram status messages. Returns
    (groups_updated, listings_in_window, listings_pruned)."""
    hours = window_seconds / 3600.0
    cutoff = time.time() - window_seconds
    if announce:
        notifier.send_text(
            f"\U0001F504 Updating price database — pruning to the last "
            f"{hours:.0f}h and recomputing market medians…"
        )
    pruned, _hist = store.prune_listings_older_than(cutoff)
    groups, listings = store.refresh_medians_since(
        cutoff, bottom_percentile, min_rows=min_rows,
    )
    store.commit()
    log.info(
        "Median refresh: pruned %d old listing(s); updated %d group(s) from "
        "%d listing(s) in the last %.0fh.", pruned, groups, listings, hours,
    )
    if announce:
        notifier.send_text(
            f"✅ Price database updated: {groups} market group(s) from "
            f"{listings} listing(s) (last {hours:.0f}h); pruned {pruned} old."
        )
    return groups, listings, pruned


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    notifier = build_notifier(cfg.telegram)
    for site in cfg.sites:
        if not site.enabled:
            continue
        store = open_site_store(cfg.db_dir, site.name)
        try:
            refresh_medians(
                store, notifier, cfg.evaluator.bottom_percentile,
                window_seconds=cfg.retention_window_seconds,
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
