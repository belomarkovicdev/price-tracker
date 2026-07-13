"""Politeness controls for outbound requests.

Two layers:

1. RateLimiter  — the *tuning* layer. A configurable minimum delay (plus random
   jitter) between requests so we look like a human browsing, not a script.

2. CircuitBreaker — the *safety* layer. A HARDCODED backstop: no matter what the
   config or a bug says, we will never send more than MAX_REQUESTS_PER_MINUTE
   requests to a single site inside any rolling 60s window. This is intentionally
   NOT configurable — it exists to protect us (and the site) if the tuning layer
   is misconfigured, a retry loop misbehaves, or concurrency sneaks in. At the
   normal 20s poll cadence we do ~3 req/min, so this should never trip in normal
   operation; if it does, something is wrong and we back off hard.
"""

from __future__ import annotations

import logging
import random
import time
from collections import deque

log = logging.getLogger(__name__)

# --- HARDCODED SAFETY INVARIANT — do not expose as config ---
# "1 request/second is normal human behaviour; never more than 60 in a minute."
MAX_REQUESTS_PER_MINUTE = 60
_WINDOW_SECONDS = 60.0
# When tripped, sit out this long before allowing traffic to this site again.
_TRIP_COOLDOWN_SECONDS = 120.0


class CircuitBreakerTripped(RuntimeError):
    """Raised when a site exceeded the hardcoded per-minute request ceiling."""


class CircuitBreaker:
    """Per-site rolling-window request ceiling. Shared-nothing per site."""

    def __init__(self, site: str) -> None:
        self.site = site
        self._times: deque[float] = deque()
        self._tripped_until: float = 0.0

    def _now(self) -> float:
        return time.monotonic()

    def before_request(self) -> None:
        now = self._now()

        if now < self._tripped_until:
            raise CircuitBreakerTripped(
                f"[{self.site}] circuit breaker cooling down "
                f"({self._tripped_until - now:.0f}s left)"
            )

        # Drop timestamps older than the window.
        cutoff = now - _WINDOW_SECONDS
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

        if len(self._times) >= MAX_REQUESTS_PER_MINUTE:
            self._tripped_until = now + _TRIP_COOLDOWN_SECONDS
            log.error(
                "[%s] CIRCUIT BREAKER TRIPPED: %d requests in <60s. "
                "Backing off %.0fs. This indicates a bug or misconfiguration.",
                self.site, len(self._times), _TRIP_COOLDOWN_SECONDS,
            )
            raise CircuitBreakerTripped(
                f"[{self.site}] exceeded {MAX_REQUESTS_PER_MINUTE} req/min ceiling"
            )

        self._times.append(now)


class RateLimiter:
    """Enforces a minimum (jittered) delay between successive requests."""

    def __init__(self, min_delay: float, jitter: float) -> None:
        self.min_delay = max(0.0, min_delay)
        self.jitter = max(0.0, jitter)
        self._last: float | None = None

    def wait(self) -> None:
        target = self.min_delay + random.uniform(0, self.jitter)
        if self._last is not None:
            elapsed = time.monotonic() - self._last
            remaining = target - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()
