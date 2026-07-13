"""Scraper base class and registry.

Every site is a subclass of Scraper. The engine only ever talks to this
interface, so adding Kleinanzeigen or another site later means writing one new
subclass and registering it — nothing else in the pipeline changes.

The base class owns the shared HTTP path: it routes every request through the
site's CircuitBreaker (hard safety) and RateLimiter (polite spacing), sets
sane browser-like headers, and detects block signals. Subclasses implement only
`fetch_listings()`.
"""

from __future__ import annotations

import logging
from typing import Callable

import requests

from ..config import SiteConfig
from ..models import Listing
from ..ratelimit import CircuitBreaker, RateLimiter

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type["Scraper"]] = {}


def register(name: str) -> Callable[[type["Scraper"]], type["Scraper"]]:
    def deco(cls: type["Scraper"]) -> type["Scraper"]:
        _REGISTRY[name] = cls
        return cls
    return deco


def build_scraper(cfg: SiteConfig) -> "Scraper | None":
    cls = _REGISTRY.get(cfg.name)
    if cls is None:
        log.warning("No scraper registered for site %r; skipping.", cfg.name)
        return None
    return cls(cfg)


class BlockedError(RuntimeError):
    """Raised when the site appears to be blocking/challenging us."""


class Scraper:
    #: subclasses set this to their site key (must match config + @register)
    site: str = ""

    #: default browser-like headers; subclasses can extend
    headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.6",
    }

    def __init__(self, cfg: SiteConfig) -> None:
        self.cfg = cfg
        self.breaker = CircuitBreaker(cfg.name)
        self.limiter = RateLimiter(cfg.request_delay_seconds, cfg.jitter_seconds)
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    # -- shared HTTP path ---------------------------------------------------
    def get(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL politely and safely. Raises CircuitBreakerTripped if the
        hard ceiling is hit, BlockedError on block signals."""
        self.breaker.before_request()   # hard safety ceiling (may raise)
        self.limiter.wait()             # polite spacing
        resp = self.session.get(url, timeout=30, **kwargs)
        self._detect_block(resp)
        return resp

    def _detect_block(self, resp: requests.Response) -> None:
        if resp.status_code in (403, 429):
            raise BlockedError(f"[{self.site}] HTTP {resp.status_code} (blocked/rate-limited)")
        if resp.status_code >= 500:
            raise BlockedError(f"[{self.site}] HTTP {resp.status_code} (server error)")
        low = resp.text[:2000].lower()
        if "captcha" in low or "are you a robot" in low or "unusual traffic" in low:
            raise BlockedError(f"[{self.site}] challenge page detected")

    # -- subclass API -------------------------------------------------------
    def fetch_listings(
        self, search_name: str, url: str,
        start_page: int = 1, num_pages: int = 1,
    ) -> list[Listing]:
        """Return listings for a search, fetching `num_pages` pages starting at
        `start_page`. The engine skips the leading paid pages by starting deeper,
        and seeds a wider range on first run."""
        raise NotImplementedError
