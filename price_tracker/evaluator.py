"""Decide whether a listing is a below-average deal worth alerting on.

Approach (robust to outliers and scams):
  * Compare only within a like-for-like bucket (see Listing.bucket()).
  * Use the ROLLING WINDOW of the most recent `window_rows` comparable prices,
    recomputed live each time.
  * Use MEDIAN + MAD, not mean/stddev — one absurd listing can't skew the median.
  * A listing is a deal when its price is meaningfully below the pack:
        price <= median - mad_k * MAD        (statistically cheap), OR
        price <= bottom_percentile of window (cheapest slice),
    AND the discount vs median is at least `min_deal_discount`.
  * Scam/typo/parts guard: ignore prices that are absurdly low
    (< scam_floor_ratio * median) or below an absolute floor.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

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


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


class Evaluator:
    def __init__(self, cfg: EvaluatorConfig) -> None:
        self.cfg = cfg

    def evaluate(self, listing: Listing, comparables: list[float]) -> Verdict:
        cfg = self.cfg
        price = listing.price

        if price is None or price <= 0:
            return Verdict(False, "no price")
        if listing.bucket() is None:
            return Verdict(False, "not enough attributes to compare")
        if price < cfg.min_price:
            return Verdict(False, f"below absolute floor ({cfg.min_price})", price)

        sample = [p for p in comparables if p and p > 0]
        n = len(sample)
        if n < cfg.min_samples:
            return Verdict(False, f"thin bucket ({n}<{cfg.min_samples} comparables)",
                           price, sample_size=n)

        median = statistics.median(sample)
        discount = (median - price) / median if median else 0.0

        # Scam / typo / for-parts guard.
        if price < cfg.scam_floor_ratio * median:
            return Verdict(False, "suspiciously low (scam/parts guard)",
                           price, median, n, discount)

        # MAD (median absolute deviation) — robust spread measure.
        deviations = [abs(p - median) for p in sample]
        mad = statistics.median(deviations)
        mad_threshold = median - cfg.mad_k * mad if mad > 0 else median
        pct_threshold = _percentile(sorted(sample), cfg.bottom_percentile)

        statistically_cheap = price <= mad_threshold or price <= pct_threshold
        meaningful = discount >= cfg.min_deal_discount

        if statistically_cheap and meaningful:
            return Verdict(
                True,
                f"{discount * 100:.0f}% below median of {n} comparables",
                price, median, n, discount,
            )
        if not meaningful:
            return Verdict(False, f"only {discount * 100:.0f}% below median",
                           price, median, n, discount)
        return Verdict(False, "within normal spread", price, median, n, discount)
