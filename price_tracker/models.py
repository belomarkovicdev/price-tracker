"""Site-agnostic data model shared across scrapers, store, evaluator, notifier."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Listing:
    """A single normalized listing. Every scraper must produce these, so the
    rest of the pipeline (store/evaluator/notifier) never needs to know which
    site the data came from."""

    site: str                       # e.g. "polovniautomobili"
    listing_id: str                 # site-unique id (string)
    search_name: str                # which configured search surfaced it
    url: str
    title: str
    price: Optional[float]          # in `currency`
    currency: str = "EUR"

    # Structured comparables (mainly cars; None for non-car sites/goods).
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    mileage: Optional[int] = None   # km
    fuel: Optional[str] = None
    gearbox: Optional[str] = None   # normalized: "manual" / "auto" / raw
    engine_cc: Optional[int] = None
    power_kw: Optional[int] = None
    city: Optional[str] = None

    status: str = "active"
    featured: bool = False          # promoted/sponsored placement
    image: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Globally unique key across sites."""
        return f"{self.site}:{self.listing_id}"

    def bucket(self) -> Optional[str]:
        """Like-for-like comparable bucket. Two listings share a bucket only if
        it's fair to compare their prices. Returns None when we lack the
        attributes to compare fairly (then the listing can't be judged)."""
        if not (self.brand and self.model and self.year and self.price):
            return None
        year_band = (self.year // 2) * 2                       # 2-year bins
        if self.mileage is None:
            mile_band = "na"
        else:
            mile_band = str(self.mileage // 25000)             # 25k km bins
        fuel = (self.fuel or "na").strip().lower()
        gb = normalize_gearbox(self.gearbox)
        return f"{self.site}|{self.brand}|{self.model}|{year_band}|{mile_band}|{fuel}|{gb}".lower()

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


def normalize_gearbox(value: Optional[str]) -> str:
    if not value:
        return "na"
    v = value.strip().lower()
    if "auto" in v or "dsg" in v or "tiptronic" in v:
        return "auto"
    if "manu" in v or "ručni" in v or "rucni" in v:
        return "manual"
    return re.sub(r"\s+", "-", v)
