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
        median = f"{v.median:,.0f} {l.currency}" if v.median is not None else "?"
        return (
            f"\U0001F525 DEAL — {v.discount * 100:.0f}% below market\n"
            f"{l.title}\n"
            f"{specs}\n\n"
            f"Price: {price}\n"
            f"Market median: {median}  (n={v.sample_size})\n"
            f"{l.url}"
        )


class Notifier:
    def send(self, note: Notification) -> bool:
        raise NotImplementedError

    def send_text(self, text: str) -> bool:
        """Send a plain status/heartbeat message not tied to a deal (e.g. the
        hourly 'DB is being updated' notice). Separate from send() because that
        one renders a Notification; this takes arbitrary text."""
        raise NotImplementedError
