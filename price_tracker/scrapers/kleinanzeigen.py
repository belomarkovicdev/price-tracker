"""kleinanzeigen.de scraper.

Unlike polovniautomobili, Kleinanzeigen is a server-rendered HTML site with no
embedded JSON blob, and — crucially — its search-results cards carry only a
free-text title, price, mileage and registration date. The structured fields we
group and compare on (Marke, Modell, Kraftstoffart, Erstzulassung) live ONLY on
each ad's own detail page.

So this scraper works in two steps:
  1. Parse the cheap results page for each ad's id, url, title, price, location
     and featured flag.
  2. For a genuinely NEW ad (one the store has never seen), fetch its detail page
     once to read the structured attributes. Ads we already know reuse their
     stored attributes via the `stored_attrs` hook — we never re-fetch a detail
     page. In steady "watch for new posts" mode that's a handful of extra
     requests per cycle, all spaced by the same polite rate limiter.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Callable, Optional

from ..models import Listing
from .base import BlockedError, Scraper, register

log = logging.getLogger(__name__)

_BASE = "https://www.kleinanzeigen.de"

# Every genuine search-results page — even one with zero matching ads — renders
# the results container. A soft block / bot challenge / interstitial returns
# HTTP 200 WITHOUT it, which would otherwise parse to zero cards and be silently
# logged as "0 listings". Its presence is what tells a real (possibly empty)
# result set apart from a page we were never actually served.
_RESULTS_CONTAINER = "srchrslt-adtable"

# Each result is a <li class="ad-listitem ..."> wrapping an
# <article class="aditem" data-adid=".." data-href="..">. The li class carries
# the promoted marker (is-topad); the article carries the id + relative url.
_ITEM_RE = re.compile(
    r'<article class="aditem"\s+data-adid="(\d+)"\s+data-href="([^"]+)"', re.S
)
_TITLE_RE = re.compile(r'<a class="ellipsis"[^>]*>(.*?)</a>', re.S)
_PRICE_RE = re.compile(
    r'price-shipping--price">\s*(.*?)\s*</p>', re.S
)
_LOC_RE = re.compile(
    r'aditem-main--top--left">(.*?)</div>', re.S
)

# Detail-page attribute list: <li class="addetailslist--detail">Label
# <span class="addetailslist--detail--value">Value</span></li>.
_DETAIL_RE = re.compile(
    r'<li class="addetailslist--detail[^"]*">(.*?)</li>', re.S
)
_DETAIL_VALUE_RE = re.compile(
    r'<span class="addetailslist--detail--value"[^>]*>(.*?)</span>', re.S
)


@register("kleinanzeigen")
class KleinanzeigenScraper(Scraper):
    site = "kleinanzeigen"

    # German, so ask for German content.
    headers = {
        **Scraper.headers,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    }

    def fetch_listings(
        self, search_name: str, url: str,
        start_page: int = 1, num_pages: int = 1,
        stored_attrs: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> list[Listing]:
        listings: list[Listing] = []
        # Cap detail-page fetches per cycle so a burst of new ads (e.g. 27 on
        # first sight) doesn't fire a rapid run of requests that trips a soft
        # block. 0 = unlimited. Over-budget new ads are simply left for a later
        # cycle: we skip them here without storing them, so they stay "new" and
        # get fetched next time — draining the backlog at cap/cycle.
        cap = getattr(self.cfg, "max_detail_fetches_per_cycle", 0)
        fetched = deferred = 0
        last_page = start_page + max(1, num_pages) - 1
        for page in range(start_page, last_page + 1):
            resp = self.get(_page_url(url, page))
            if _RESULTS_CONTAINER not in resp.text:
                # HTTP 200 but not a real results page: soft block / bot
                # challenge / interstitial (common from datacenter IPs, and not
                # caught by the shared status/keyword block detector). Surface
                # it as a block instead of silently reporting "0 listings" —
                # the engine logs it and backs off for the cycle.
                raise BlockedError(
                    f"[{self.site}] no results container on page {page} "
                    "(soft block / challenge / layout change?)"
                )
            cards = _parse_cards(resp.text)
            if not cards:
                break   # empty / past the last page
            for card in cards:
                # Reuse structured attrs for ads we've already enriched; only pay
                # a detail-page fetch on genuinely new ads.
                attrs = stored_attrs(card["adid"]) if stored_attrs else None
                if attrs is None:
                    if cap and fetched >= cap:
                        deferred += 1
                        continue   # over budget — leave unseen for next cycle
                    attrs = self._fetch_detail(card["href"])
                    fetched += 1
                listings.append(self._build_listing(search_name, card, attrs))
        if deferred:
            log.info(
                "[%s] %r: deferred %d new ad(s) past the per-cycle detail cap "
                "(%d); they'll be fetched in an upcoming cycle.",
                self.site, search_name, deferred, cap,
            )
        return listings

    def _build_listing(
        self, search_name: str, card: dict[str, Any], attrs: dict[str, Any],
    ) -> Listing:
        adid = card["adid"]
        return Listing(
            site=self.site,
            listing_id=adid,
            search_name=search_name,
            url=_BASE + card["href"],
            title=card["title"],
            price=card["price"],
            currency="EUR",
            brand=attrs.get("brand"),
            model=attrs.get("model"),
            year=attrs.get("year"),
            mileage=attrs.get("mileage"),
            fuel=attrs.get("fuel"),
            gearbox=attrs.get("gearbox"),
            engine_cc=attrs.get("engine_cc"),
            power_kw=attrs.get("power_kw"),
            city=attrs.get("city") or card.get("city"),
            status="active",
            featured=card["featured"],
            image=card.get("image"),
            raw={"card": card, "detail": attrs},
        )

    def _fetch_detail(self, href: str) -> dict[str, Any]:
        """Fetch one ad's detail page and pull its structured attributes. A
        removed/404 ad just yields an empty dict (stored without comparables);
        real block signals propagate up and skip the cycle."""
        resp = self.get(_BASE + href)
        return _parse_detail(resp.text)


# -- results-page parsing --------------------------------------------------
def _parse_cards(html_text: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    # One <li class="ad-listitem ..."> per result; the li class holds the
    # promoted marker, so split on it to keep each card with its wrapper class.
    for block in html_text.split('<li class="ad-listitem'):
        m = _ITEM_RE.search(block)
        if not m:
            continue
        adid, href = m.group(1), m.group(2)
        title_m = _TITLE_RE.search(block)
        price_m = _PRICE_RE.search(block)
        loc_m = _LOC_RE.search(block)
        # 'is-topad' appears in the li class, before the <article> tag.
        prefix = block[: m.start()]
        cards.append({
            "adid": adid,
            "href": href,
            "title": _text(title_m.group(1)) if title_m else "",
            "price": _price(price_m.group(1)) if price_m else None,
            "city": _text(loc_m.group(1)) if loc_m else None,
            "featured": "is-topad" in prefix,
        })
    return cards


# -- detail-page parsing ---------------------------------------------------
def _parse_detail(html_text: str) -> dict[str, Any]:
    raw: dict[str, str] = {}
    for m in _DETAIL_RE.finditer(html_text):
        block = m.group(1)
        vm = _DETAIL_VALUE_RE.search(block)
        if not vm:
            continue
        label = _text(block[: vm.start()]).rstrip(":")
        raw[label] = _text(vm.group(1))

    attrs: dict[str, Any] = {}
    attrs["brand"] = raw.get("Marke")
    attrs["model"] = raw.get("Modell")
    attrs["fuel"] = raw.get("Kraftstoffart")
    attrs["year"] = _year(raw.get("Erstzulassung"))
    attrs["mileage"] = _int(raw.get("Kilometerstand"))
    attrs["power_kw"] = _power_kw(raw.get("Leistung"))
    attrs["gearbox"] = _gearbox(raw.get("Getriebe"))
    return attrs


# -- helpers ---------------------------------------------------------------
def _page_url(url: str, page: int) -> str:
    """Kleinanzeigen paginates by inserting `/seite:N/` before the category
    segment (…/preis:2000:/seite:2/c216l7190r100+…). Page 1 is the bare url."""
    if page <= 1:
        return url
    new, n = re.subn(r'/(c\d+l\d+)', rf'/seite:{page}/\1', url, count=1)
    return new if n else url


def _text(s: str | None) -> str:
    if not s:
        return ""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _price(text: str | None) -> Optional[float]:
    """'14.100 € VB' -> 14100.0. Returns None for 'VB' / 'Preis auf Anfrage' /
    'Zu verschenken' (no numeric price to compare)."""
    m = re.search(r"(\d[\d.]*)", _text(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _int(text: str | None) -> Optional[int]:
    """'217.000 km' -> 217000."""
    m = re.search(r"(\d[\d.]*)", _text(text))
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _year(text: str | None) -> Optional[int]:
    """'Januar 2014' / '01/2014' -> 2014."""
    m = re.search(r"(\d{4})", _text(text))
    return int(m.group(1)) if m else None


def _power_kw(text: str | None) -> Optional[int]:
    """Leistung is given in PS ('183 PS'); convert to kW (1 PS ≈ 0.7355 kW) so it
    lines up with the shared power_kw field."""
    m = re.search(r"(\d+)\s*PS", _text(text))
    if not m:
        return None
    return round(int(m.group(1)) * 0.7355)


def _gearbox(text: str | None) -> Optional[str]:
    t = _text(text).lower()
    if not t:
        return None
    if "automat" in t:
        return "auto"
    if "schalt" in t or "manuel" in t:
        return "manual"
    return t
