"""polovniautomobili.com scraper.

The site is a Next.js app: every search-results page embeds the full, structured
listing data as JSON in a `<script id="__NEXT_DATA__">` tag. We parse that JSON
directly instead of scraping fragile HTML/CSS — it's far more robust (survives
styling changes) and gives us clean typed fields. Plain HTTP works; no browser
or proxy is needed for this site.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models import Listing
from .base import Scraper, register

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
# Each listing is an <article data-testid="featuredAd"|"emptyAd"> block; the
# JSON `featuredSearch` flag is unreliable (true for every result), but this
# DOM marker distinguishes paid/promoted ("featuredAd") from regular private
# ("emptyAd") ads on every page. We map the marker back to the listing id.
_ARTICLE_RE = re.compile(r'<article\b[^>]*?data-testid="(featuredAd|emptyAd)"', re.S)
_ID_IN_ARTICLE_RE = re.compile(r'/auto-oglasi/(\d+)')
_BASE = "https://www.polovniautomobili.com"


@register("polovniautomobili")
class PolovniScraper(Scraper):
    site = "polovniautomobili"

    def fetch_listings(
        self, search_name: str, url: str,
        start_page: int = 1, num_pages: int = 1,
        stored_attrs=None,   # unused: the list page already carries full details
    ) -> list[Listing]:
        listings: list[Listing] = []
        last_page = start_page + max(1, num_pages) - 1
        for page in range(start_page, last_page + 1):
            page_url = url if page == 1 else _with_page(url, page)
            resp = self.get(page_url)
            results, page_count = self._parse_page(resp.text, search_name)
            featured_ids = _featured_ids(resp.text)
            for r in results:
                listing = self._to_listing(search_name, r, featured_ids)
                if listing is not None:
                    listings.append(listing)
            # Stop early: empty page, or we've reached the last page.
            if not results:
                break
            if page_count and page >= page_count:
                break
        return listings

    def _parse_page(self, html: str, search_name: str):
        m = _NEXT_DATA_RE.search(html)
        if not m:
            log.warning("[%s] no __NEXT_DATA__ for search %r", self.site, search_name)
            return [], None
        try:
            sr = json.loads(m.group(1))["props"]["pageProps"]["searchResults"]
            return sr.get("results", []), sr.get("pageCount")
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("[%s] could not parse listings JSON: %s", self.site, exc)
            return [], None

    def _to_listing(
        self, search_name: str, r: dict[str, Any], featured_ids: set[str]
    ) -> Listing | None:
        listing_id = r.get("id")
        if listing_id is None:
            return None
        listing_id = str(listing_id)
        price = r.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None

        currency = "EUR" if r.get("priceCurrency") in ("€", "EUR", None) else str(r.get("priceCurrency"))

        return Listing(
            site=self.site,
            listing_id=listing_id,
            search_name=search_name,
            url=f"{_BASE}/auto-oglasi/{listing_id}/pt",
            title=str(r.get("title") or "").strip(),
            price=price,
            currency=currency,
            brand=_clean(r.get("brand")),
            model=_clean(r.get("model")),
            year=_int(r.get("year")),
            mileage=_int(r.get("mileage")),
            fuel=_clean(r.get("fuel")),
            gearbox=_clean(r.get("gearBox")),
            engine_cc=_int(r.get("engineVolume")),
            power_kw=_int(r.get("power")),
            city=_clean(r.get("city")),
            status=str(r.get("status") or "active"),
            # Paid/promoted vs regular, from the reliable DOM marker.
            featured=listing_id in featured_ids,
            image=r.get("imageMain"),
            raw=r,
        )


def _featured_ids(html: str) -> set[str]:
    """Set of listing ids whose <article> is marked data-testid="featuredAd"
    (paid/promoted). Regular ads are "emptyAd" and are not included."""
    ids: set[str] = set()
    # Walk each <article ...> block and, if it's a featuredAd, grab the first
    # listing id inside it.
    for m in _ARTICLE_RE.finditer(html):
        if m.group(1) != "featuredAd":
            continue
        block = html[m.start(): m.start() + 3000]
        idm = _ID_IN_ARTICLE_RE.search(block)
        if idm:
            ids.add(idm.group(1))
    return ids


def _with_page(url: str, page: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
