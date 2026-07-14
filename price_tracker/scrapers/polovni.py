"""polovniautomobili.com scraper.

The site is a Next.js app: every search-results page embeds the full, structured
listing data as JSON in a `<script id="__NEXT_DATA__">` tag. We parse that JSON
directly instead of scraping fragile HTML/CSS — it's far more robust (survives
styling changes) and gives us clean typed fields. Plain HTTP works; no browser
is needed for this site.
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
# ("emptyAd") ads on every page. We map the marker back to the listing id and
# keep ONLY the emptyAds: the featuredAds are the same promoted listings pinned
# to the top of every page, so they duplicate across pages and inflate stats.
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
            empty_ids = _empty_ad_ids(resp.text)
            for r in results:
                # Skip paid/promoted ads entirely: keep only regular "emptyAd"
                # private listings.
                if str(r.get("id")) not in empty_ids:
                    continue
                listing = self._to_listing(search_name, r)
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
        self, search_name: str, r: dict[str, Any]
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

        # Persist ONLY what the median/deal logic needs: brand, model, year, fuel
        # and price (+ its currency). The record still needs an id to dedup on,
        # a url/title for the alert message, and status for the active-filter —
        # those are structural, not ad detail. Everything else (mileage, gearbox,
        # engine, power, city, image) and the full raw JSON blob are deliberately
        # NOT stored; Listing's remaining fields keep their None/empty defaults.
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
            fuel=_clean(r.get("fuel")),
            status=str(r.get("status") or "active"),
        )


def _empty_ad_ids(html: str) -> set[str]:
    """Set of listing ids whose <article> is marked data-testid="emptyAd"
    (regular private ads). Paid/promoted ads are "featuredAd" and are excluded,
    so the caller keeps only these."""
    ids: set[str] = set()
    # Walk each <article ...> block, bounded by the next article, and if it's an
    # emptyAd grab the first listing id inside it.
    matches = list(_ARTICLE_RE.finditer(html))
    for i, m in enumerate(matches):
        if m.group(1) != "emptyAd":
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else m.start() + 3000
        block = html[m.start(): end]
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
