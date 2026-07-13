"""Decide whether a listing is a below-average deal worth alerting on.

Approach:
  * Each brand+model+year has a single stored average price (see the
    model_prices table), refreshed at most once per 24h.
  * A listing is a deal when its price is meaningfully below that average:
    the discount vs the average is at least `min_deal_discount`.
  * Scam/typo/parts guard: ignore prices that are absurdly low
    (< scam_floor_ratio * avg) or below an absolute floor (`min_price`).
  * Don't judge against a thin average (fewer than `min_samples` listings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import EvaluatorConfig
from .models import Listing


@dataclass
class Verdict:
    is_deal: bool
    reason: str
    price: Optional[float] = None
    avg: Optional[float] = None
    sample_size: int = 0
    discount: float = 0.0          # fraction below avg, e.g. 0.18 = 18% cheaper


class Evaluator:
    def __init__(self, cfg: EvaluatorConfig) -> None:
        self.cfg = cfg

    def evaluate(
        self, listing: Listing, avg_price: Optional[float], sample_count: int
    ) -> Verdict:
        """Compare the listing's price to the stored average price for its
        brand+model+year (a single number), not a live window of comparables."""
        cfg = self.cfg
        price = listing.price

        if price is None or price <= 0:
            return Verdict(False, "no price")
        if not (listing.brand and listing.model and listing.year):
            return Verdict(False, "not enough attributes to compare")
        if price < cfg.min_price:
            return Verdict(False, f"below absolute floor ({cfg.min_price})", price)
        if not avg_price or sample_count < cfg.min_samples:
            return Verdict(
                False,
                f"thin data ({sample_count}<{cfg.min_samples} listings)",
                price, sample_size=sample_count,
            )

        avg = avg_price
        discount = (avg - price) / avg if avg else 0.0

        # Scam / typo / for-parts guard.
        if price < cfg.scam_floor_ratio * avg:
            return Verdict(False, "suspiciously low (scam/parts guard)",
                           price, avg, sample_count, discount)

        if discount >= cfg.min_deal_discount:
            return Verdict(
                True,
                f"{discount * 100:.0f}% below avg of {sample_count} listings",
                price, avg, sample_count, discount,
            )
        return Verdict(False, f"only {discount * 100:.0f}% below avg",
                       price, avg, sample_count, discount)
