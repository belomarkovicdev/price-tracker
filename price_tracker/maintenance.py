"""Hourly maintenance: prune the in-memory buffer and rebuild the stored medians.

The db is just the aggregate now — one median row per (site, brand, model, year,
fuel). Every hour, for each site, we prune the buffer to the retention window
(default 24h), rebuild that site's `model_prices` from what's left (only groups
with more than 4 comparables), and post a Telegram heartbeat around it. So the
medians reflect the recent market, and the db stays tiny.

This runs from inside the engine loop (which holds the buffers). There is no
standalone CLI: the buffer is process-local, so medians can only be recomputed
by the running tracker.
"""

from __future__ import annotations

import logging
import time

from .buffer import ListingBuffer
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)


def refresh_medians(
    store: Store,
    buffer: ListingBuffer,
    notifier: Notifier,
    bottom_percentile: float,
    window_seconds: float = 24 * 3600.0,
    announce: bool = True,
    min_rows: int = 11,
) -> tuple[int, int, int]:
    """Prune `buffer` to the rolling window, then rebuild `store`'s medians from
    it (only groups with at least `min_rows` prices; default 11 → "more than
    10"). If `announce`, bracket it with Telegram status messages. Returns
    (groups_written, prices_used, pruned)."""
    hours = window_seconds / 3600.0
    if announce:
        notifier.send_text(
            f"\U0001F504 Updating price database — pruning to the last "
            f"{hours:.0f}h and recomputing market medians…"
        )
    pruned = buffer.prune(time.time() - window_seconds)
    groups, used = store.rebuild_model_prices(
        buffer.group_prices(), bottom_percentile, min_rows=min_rows,
    )
    store.commit()
    log.info(
        "Median refresh: pruned %d old listing(s); wrote %d group(s) from "
        "%d listing(s) in the last %.0fh (buffer now %d).",
        pruned, groups, used, hours, len(buffer),
    )
    if announce:
        notifier.send_text(
            f"✅ Price database updated: {groups} market group(s) from "
            f"{used} listing(s) (last {hours:.0f}h); pruned {pruned} old."
        )
    return groups, used, pruned
