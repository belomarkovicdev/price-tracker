"""Notifier interface and the message we build from a deal."""

from __future__ import annotations

from dataclasses import dataclass

from ..evaluator import Verdict
from ..models import Listing


@dataclass
class Notification:
    listing: Listing
    verdict: Verdict

    def as_text(self) -> str:
        l = self.listing
        v = self.verdict
        specs = " · ".join(
            str(x) for x in [
                l.year,
                f"{l.mileage:,} km" if l.mileage else None,
                l.fuel,
                l.gearbox,
                l.city,
            ] if x
        )
        price = f"{l.price:,.0f} {l.currency}" if l.price is not None else "?"
        avg = f"{v.avg:,.0f} {l.currency}" if v.avg is not None else "?"
        return (
            f"\U0001F525 DEAL — {v.discount * 100:.0f}% below market\n"
            f"{l.title}\n"
            f"{specs}\n\n"
            f"Price: {price}\n"
            f"Market avg: {avg}  (n={v.sample_size})\n"
            f"{l.url}"
        )


class Notifier:
    def send(self, note: Notification) -> bool:
        raise NotImplementedError
