"""Hourly DB maintenance: recompute the stored market medians and announce it.

The median/MAD/low-percentile in `model_prices` is a *derived cache*. During
normal scanning it refreshes at most once per 24h, because recomputing it reads
a group's whole sample and we don't want that on every 20s cycle. This module
forces a fresh recompute for every group that received a listing in the last
window (default the past hour) and posts a Telegram heartbeat around it, so the
medians track recent listings instead of lagging up to a day behind.

It runs two ways, sharing the same `refresh_medians()`:
  * from inside the engine loop, every `median_refresh_interval_seconds`; and
  * standalone via `python -m price_tracker.maintenance` — for a one-off run,
    or to drive it from an external scheduler (Windows Task Scheduler / cron)
    if you don't keep `run.py` running 24/7.
"""

from __future__ import annotations

import logging
import time

from .config import load_config
from .notify import Notifier
from .notify.telegram import build_notifier
from .store import Store

log = logging.getLogger(__name__)

# Only (re)compute a group's median once it has MORE THAN 4 comparables — a
# median over a handful of listings isn't meaningful. `min_rows` is the minimum
# row count to update, so "more than 4" is 5.
_MIN_ROWS_TO_UPDATE = 5


def refresh_medians(
    store: Store,
    notifier: Notifier,
    bottom_percentile: float,
    since_seconds: float = 3600.0,
    announce: bool = True,
    min_rows: int = _MIN_ROWS_TO_UPDATE,
) -> tuple[int, int]:
    """Recompute stored medians for groups seen in the last `since_seconds` that
    have at least `min_rows` comparables and, if `announce`, bracket it with
    Telegram status messages. Returns (groups_updated, listings_covered)."""
    hours = since_seconds / 3600.0
    if announce:
        notifier.send_text(
            f"\U0001F504 Updating price database — recomputing market medians "
            f"from the last {hours:.0f}h of listings…"
        )
    groups, listings = store.refresh_medians_since(
        time.time() - since_seconds, bottom_percentile, min_rows=min_rows,
    )
    store.commit()
    log.info(
        "Median refresh: %d group(s), %d listing(s) (window %.0fh).",
        groups, listings, hours,
    )
    if announce:
        notifier.send_text(
            f"✅ Price database updated: {groups} market group(s) "
            f"refreshed from {listings} listing(s)."
        )
    return groups, listings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    store = Store(cfg.db_path)
    notifier = build_notifier(cfg.telegram)
    try:
        refresh_medians(
            store, notifier, cfg.evaluator.bottom_percentile,
            since_seconds=cfg.median_refresh_interval_seconds,
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
