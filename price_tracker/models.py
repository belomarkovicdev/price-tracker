"""Site-agnostic data model shared across scrapers, store, evaluator, notifier."""

from __future__ import annotations

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

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d
