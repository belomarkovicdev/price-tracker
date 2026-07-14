"""Decide whether a listing is a below-average deal worth alerting on.

Approach (robust to outliers and scams):
  * Each group's price stats (median, MAD, low-percentile) are computed from the
    in-memory buffer (see buffer.ListingBuffer + store.price_stats) and passed in.
    The hourly refresh persists the same stats to model_prices.
  * Use MEDIAN + MAD, not mean/stddev — one absurd listing can't skew the median.
  * A listing is a deal when its price is meaningfully below the pack:
        price <= median - mad_k * MAD        (statistically cheap), OR
        price <= low-percentile of the sample (cheapest slice),
    AND the discount vs median is at least `min_deal_discount`.
  * Scam/typo/parts guard: ignore prices that are absurdly low
    (< scam_floor_ratio * median) or below an absolute floor (`min_price`).
  * Don't judge against a thin sample (fewer than `min_samples` listings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .config import EvaluatorConfig
from .models import Listing


@dataclass
class Verdict:
    is_deal: bool
    reason: str
    price: Optional[float] = None
    median: Optional[float] = None
    sample_size: int = 0
    discount: float = 0.0          # fraction below median, e.g. 0.18 = 18% cheaper


class Evaluator:
    def __init__(self, cfg: EvaluatorConfig) -> None:
        self.cfg = cfg

    def evaluate(self, listing: Listing, stats: Mapping[str, Any]) -> Verdict:
        """Compare the listing's price to the stored stats (median / MAD /
        low-percentile) for its brand+model+year — a single pre-computed row,
        not a live window recomputed per listing."""
        cfg = self.cfg
        price = listing.price

        if price is None or price <= 0:
            return Verdict(False, "no price")
        if not (listing.brand and listing.model and listing.year):
            return Verdict(False, "not enough attributes to compare")
        if price < cfg.min_price:
            return Verdict(False, f"below absolute floor ({cfg.min_price})", price)

        median = stats["median"]
        n = stats["sample_count"] or 0
        if median is None or n < cfg.min_samples:
            return Verdict(False, f"thin data ({n}<{cfg.min_samples} listings)",
                           price, sample_size=n)

        discount = (median - price) / median if median else 0.0

        # Scam / typo / for-parts guard.
        if price < cfg.scam_floor_ratio * median:
            return Verdict(False, "suspiciously low (scam/parts guard)",
                           price, median, n, discount)

        mad = stats["mad"] or 0.0
        p_low = stats["p_low"]
        mad_threshold = median - cfg.mad_k * mad if mad > 0 else median
        statistically_cheap = (price <= mad_threshold
                               or (p_low is not None and price <= p_low))
        meaningful = discount >= cfg.min_deal_discount

        if statistically_cheap and meaningful:
            return Verdict(
                True,
                f"{discount * 100:.0f}% below median of {n} listings",
                price, median, n, discount,
            )
        if not meaningful:
            return Verdict(False, f"only {discount * 100:.0f}% below median",
                           price, median, n, discount)
        return Verdict(False, "within normal spread", price, median, n, discount)
