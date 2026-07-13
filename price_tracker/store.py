"""SQLite persistence.

Why persistence at all: the "below average" judgement needs a corpus of
comparable listings, and one fetched page only shows ~25. By accumulating every
listing we see, the comparable set grows over time while we keep fetching just
one light page per cycle. It also lets us dedup (don't re-alert) across restarts
and detect price drops.

SQLite is used because it's in the Python standard library (zero extra
dependency), survives restarts, and indexes the per-bucket "give me the most
recent N comparables" query the evaluator needs.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Optional

from .models import Listing


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key          TEXT PRIMARY KEY,
    site         TEXT NOT NULL,
    listing_id   TEXT NOT NULL,
    search_name  TEXT,
    url          TEXT,
    title        TEXT,
    price        REAL,
    currency     TEXT,
    brand        TEXT,
    model        TEXT,
    year         INTEGER,
    mileage      INTEGER,
    fuel         TEXT,
    gearbox      TEXT,
    engine_cc    INTEGER,
    power_kw     INTEGER,
    city         TEXT,
    status       TEXT,
    featured     INTEGER,
    image        TEXT,
    bucket       TEXT,
    raw          TEXT,
    first_seen   REAL,
    last_seen    REAL
);
CREATE INDEX IF NOT EXISTS idx_listings_bucket ON listings(bucket, last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL,
    price    REAL,
    seen_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_price_history_key ON price_history(key);

CREATE TABLE IF NOT EXISTS alerts (
    key           TEXT PRIMARY KEY,
    alerted_price REAL,
    alerted_at    REAL
);

-- Persisted market average per like-for-like bucket (regular ads only).
-- Recomputed each scan; query per bucket, or GROUP BY brand/model for a
-- per-model view.
CREATE TABLE IF NOT EXISTS market_stats (
    bucket        TEXT PRIMARY KEY,
    brand         TEXT,
    model         TEXT,
    sample_count  INTEGER,
    median        REAL,
    avg           REAL,
    mad           REAL,
    p_low         REAL,
    min_price     REAL,
    max_price     REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_market_stats_model ON market_stats(brand, model);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- upsert -------------------------------------------------------------
    def upsert(self, listing: Listing) -> tuple[bool, Optional[float]]:
        """Insert or update a listing. Returns (is_new, previous_price).
        Records a price_history row on first sight and on any price change."""
        now = time.time()
        cur = self.conn.execute(
            "SELECT price FROM listings WHERE key = ?", (listing.key,)
        )
        row = cur.fetchone()
        is_new = row is None
        prev_price = None if is_new else row["price"]

        self.conn.execute(
            """
            INSERT INTO listings (key, site, listing_id, search_name, url, title,
                price, currency, brand, model, year, mileage, fuel, gearbox,
                engine_cc, power_kw, city, status, featured, image, bucket, raw,
                first_seen, last_seen)
            VALUES (:key,:site,:listing_id,:search_name,:url,:title,:price,
                :currency,:brand,:model,:year,:mileage,:fuel,:gearbox,:engine_cc,
                :power_kw,:city,:status,:featured,:image,:bucket,:raw,:now,:now)
            ON CONFLICT(key) DO UPDATE SET
                search_name=excluded.search_name, url=excluded.url,
                title=excluded.title, price=excluded.price,
                status=excluded.status, featured=excluded.featured,
                image=excluded.image, bucket=excluded.bucket,
                mileage=excluded.mileage, last_seen=:now
            """,
            {
                "key": listing.key, "site": listing.site,
                "listing_id": listing.listing_id, "search_name": listing.search_name,
                "url": listing.url, "title": listing.title, "price": listing.price,
                "currency": listing.currency, "brand": listing.brand,
                "model": listing.model, "year": listing.year,
                "mileage": listing.mileage, "fuel": listing.fuel,
                "gearbox": listing.gearbox, "engine_cc": listing.engine_cc,
                "power_kw": listing.power_kw, "city": listing.city,
                "status": listing.status, "featured": int(listing.featured),
                "image": listing.image, "bucket": listing.bucket(),
                "raw": json.dumps(listing.raw, ensure_ascii=False), "now": now,
            },
        )

        if is_new or (prev_price != listing.price):
            self.conn.execute(
                "INSERT INTO price_history (key, price, seen_at) VALUES (?,?,?)",
                (listing.key, listing.price, now),
            )
        self.conn.commit()
        return is_new, prev_price

    # -- comparables --------------------------------------------------------
    def recent_bucket_prices(
        self, bucket: str, limit: int, exclude_key: Optional[str] = None
    ) -> list[float]:
        """Most recent `limit` comparable prices for a bucket (the rolling
        window). Excludes the listing under evaluation so it isn't compared
        against itself."""
        sql = (
            "SELECT price FROM listings "
            "WHERE bucket = ? AND price IS NOT NULL AND status = 'active'"
            # Includes paid + regular ads (the 'featured' flag is still recorded
            # per listing, just not used to filter the average).
        )
        params: list[object] = [bucket]
        if exclude_key:
            sql += " AND key != ?"
            params.append(exclude_key)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        return [r["price"] for r in self.conn.execute(sql, params)]

    # -- persisted market stats --------------------------------------------
    def update_bucket_stats(
        self, bucket: str, window: int, bottom_percentile: float = 20.0
    ) -> Optional[dict]:
        """Recompute the market average for a bucket from its most recent
        `window` regular listings and upsert into market_stats. Returns the
        stats (or None if the bucket has no priced regular listings)."""
        prices = self.recent_bucket_prices(bucket, window)
        if not prices:
            return None
        meta = self.conn.execute(
            "SELECT brand, model FROM listings "
            "WHERE bucket = ? ORDER BY last_seen DESC LIMIT 1",
            (bucket,),
        ).fetchone()
        median = statistics.median(prices)
        stats = {
            "bucket": bucket,
            "brand": meta["brand"] if meta else None,
            "model": meta["model"] if meta else None,
            "sample_count": len(prices),
            "median": median,
            "avg": statistics.fmean(prices),
            "mad": statistics.median([abs(p - median) for p in prices]),
            "p_low": _percentile(sorted(prices), bottom_percentile),
            "min_price": min(prices),
            "max_price": max(prices),
            "updated_at": time.time(),
        }
        self.conn.execute(
            """
            INSERT INTO market_stats (bucket, brand, model, sample_count, median,
                avg, mad, p_low, min_price, max_price, updated_at)
            VALUES (:bucket,:brand,:model,:sample_count,:median,:avg,:mad,:p_low,
                :min_price,:max_price,:updated_at)
            ON CONFLICT(bucket) DO UPDATE SET
                brand=excluded.brand, model=excluded.model,
                sample_count=excluded.sample_count, median=excluded.median,
                avg=excluded.avg, mad=excluded.mad, p_low=excluded.p_low,
                min_price=excluded.min_price, max_price=excluded.max_price,
                updated_at=excluded.updated_at
            """,
            stats,
        )
        self.conn.commit()
        return stats

    def get_bucket_stats(self, bucket: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM market_stats WHERE bucket = ?", (bucket,)
        ).fetchone()

    # -- alerts -------------------------------------------------------------
    def already_alerted(self, key: str, price: Optional[float]) -> bool:
        """True if we've already alerted this listing at this (or higher) price.
        A later *drop* re-qualifies it for a fresh alert."""
        row = self.conn.execute(
            "SELECT alerted_price FROM alerts WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return False
        if price is None or row["alerted_price"] is None:
            return True
        return price >= row["alerted_price"]

    def mark_alerted(self, key: str, price: Optional[float]) -> None:
        self.conn.execute(
            "INSERT INTO alerts (key, alerted_price, alerted_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET alerted_price=excluded.alerted_price, "
            "alerted_at=excluded.alerted_at",
            (key, price, time.time()),
        )
        self.conn.commit()
