"""In-memory rolling window of recently-seen listings, one per site.

Instead of persisting every ad to SQLite, the engine keeps the recent listings
here, in memory, and computes group medians from this buffer. Only the aggregate
(model_prices) is written to disk. Consequences of that trade-off:

  * Deduped by listing key, so a car re-seen every cycle counts once toward the
    median (this is what the old `listings` primary key did).
  * Pruned by last_seen, so it's a rolling window, not an archive.
  * Process-local and volatile: a restart starts empty and refills from the next
    seed. The persisted model_prices from the previous run stays queryable in
    the meantime.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .models import Listing


@dataclass
class _Rec:
    brand: Optional[str]
    model: Optional[str]
    year: Optional[int]
    fuel: Optional[str]
    price: Optional[float]
    last_seen: float


class ListingBuffer:
    def __init__(self) -> None:
        self._items: dict[str, _Rec] = {}

    def __len__(self) -> int:
        return len(self._items)

    def upsert(self, listing: Listing, now: float) -> None:
        """Record (or refresh) a listing, keyed by its global key so repeat
        sightings overwrite rather than duplicate."""
        self._items[listing.key] = _Rec(
            listing.brand, listing.model, listing.year, listing.fuel,
            listing.price, now,
        )

    def prune(self, cutoff_ts: float) -> int:
        """Drop entries not seen since `cutoff_ts`; return how many were removed."""
        stale = [k for k, r in self._items.items() if r.last_seen < cutoff_ts]
        for k in stale:
            del self._items[k]
        return len(stale)

    def group_prices(self) -> dict[tuple, list[float]]:
        """{(brand, model, year, fuel): [price, …]} over every fully-attributed,
        priced entry — the sample each group's median is computed from."""
        groups: dict[tuple, list[float]] = defaultdict(list)
        for r in self._items.values():
            if r.brand and r.model and r.year and r.fuel and r.price is not None:
                groups[(r.brand, r.model, r.year, r.fuel)].append(r.price)
        return groups

    def attrs(self, key: str) -> Optional[dict]:
        """Structured attrs for a known ad (or None), so a scraper can skip
        re-fetching a detail page for an ad still in the window — the in-memory
        stand-in for the old store.get_listing_attrs."""
        r = self._items.get(key)
        if r is None:
            return None
        return {"brand": r.brand, "model": r.model, "year": r.year,
                "fuel": r.fuel}
