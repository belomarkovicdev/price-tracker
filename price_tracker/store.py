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
import logging
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Optional

from .models import Listing

log = logging.getLogger(__name__)


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

-- One row per (brand, model, year) with the running average price. Unlike
-- market_stats (keyed by the fine-grained like-for-like bucket), this collapses
-- mileage/fuel/gearbox variants into a single per-model-year average. The
-- composite PRIMARY KEY guarantees we never duplicate a row for the same
-- brand+model+year — repeat sightings just update avg_price in place.
CREATE TABLE IF NOT EXISTS model_prices (
    brand        TEXT NOT NULL,
    model        TEXT NOT NULL,
    year         INTEGER NOT NULL,
    avg_price    REAL,
    median       REAL,
    mad          REAL,
    p_low        REAL,
    sample_count INTEGER,
    updated_at   REAL,
    PRIMARY KEY (brand, model, year)
);

-- Which searches have already been seeded (wide first-sight fetch). Persisted
-- so a restart trusts the corpus already in the db and skips re-seeding —
-- it goes straight to the light steady-state scan for fresh listings.
CREATE TABLE IF NOT EXISTS seed_state (
    seed_key   TEXT PRIMARY KEY,   -- "<site>:<search_name>"
    seeded_at  REAL
);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        # Ensure the parent dir exists (e.g. a mounted volume path like /data)
        # so sqlite can create the file there.
        db_path = Path(db_path)
        parent = db_path.parent
        uid = getattr(os, "getuid", lambda: "n/a")()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Could not create DB dir %s: %s", parent, exc)
        log.info(
            "Opening DB at %s (uid=%s, dir exists=%s, dir writable=%s)",
            db_path, uid, parent.exists(), os.access(parent, os.W_OK),
        )
        try:
            self.conn = sqlite3.connect(str(db_path))
        except sqlite3.OperationalError as exc:
            log.error(
                "sqlite could not open %s: %s. Running as uid=%s; dir %s "
                "writable=%s. If on a mounted volume, the volume mount path "
                "must equal the DB_PATH directory and be writable by this user.",
                db_path, exc, uid, parent, os.access(parent, os.W_OK),
            )
            raise
        self.conn.row_factory = sqlite3.Row
        # WAL + NORMAL sync: writes stay durable per commit against app crashes,
        # fsyncs get far cheaper, and readers never block the writer. We commit
        # once per search-scan (batched by the engine) rather than per listing,
        # so the many upserts in a cycle cost one fsync instead of ~125.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        # Additive migration for DBs created before model_prices grew its robust
        # stat columns. CREATE TABLE IF NOT EXISTS won't add columns to an
        # existing table, so backfill any that are missing.
        self._add_columns("model_prices",
                          {"median": "REAL", "mad": "REAL", "p_low": "REAL"})
        self.conn.commit()

    def _add_columns(self, table: str, cols: dict[str, str]) -> None:
        existing = {r["name"]
                    for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        """Flush the pending transaction. The engine calls this once per
        search-scan; the per-row write methods below deliberately do NOT commit
        so a whole scan's writes batch into a single fsync."""
        self.conn.commit()

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
        return stats

    def get_bucket_stats(self, bucket: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM market_stats WHERE bucket = ?", (bucket,)
        ).fetchone()

    # -- per model+year average --------------------------------------------
    def update_model_price(
        self, brand: str, model: str, year: int,
        bottom_percentile: float = 20.0,
        ttl_seconds: Optional[float] = None,
    ) -> Optional[dict]:
        """Recompute the price stats (avg, median, MAD, low-percentile) across
        all active, priced listings with this exact brand+model+year and upsert
        the single row for it. Returns the row (or None if there are no priced
        listings for it yet).

        The whole sample is read here — this is the one place that needs it —
        so the stored median/MAD are as robust as a live computation. Evaluation
        then reads just this one row.

        If ttl_seconds is given and the stored row was refreshed more recently
        than that, the recompute is skipped and the existing row is returned
        unchanged — so the stats refresh at most once per ttl (e.g. daily)."""
        if ttl_seconds is not None:
            existing = self.get_model_price(brand, model, year)
            if (existing is not None and existing["updated_at"] is not None
                    and time.time() - existing["updated_at"] < ttl_seconds):
                return dict(existing)
        prices = [r["price"] for r in self.conn.execute(
            "SELECT price FROM listings "
            "WHERE brand = ? AND model = ? AND year = ? "
            "AND price IS NOT NULL AND status = 'active'",
            (brand, model, year),
        )]
        if not prices:
            return None
        median = statistics.median(prices)
        stats = {
            "brand": brand, "model": model, "year": year,
            "avg_price": statistics.fmean(prices),
            "median": median,
            "mad": statistics.median([abs(p - median) for p in prices]),
            "p_low": _percentile(sorted(prices), bottom_percentile),
            "sample_count": len(prices),
            "updated_at": time.time(),
        }
        self.conn.execute(
            """
            INSERT INTO model_prices (brand, model, year, avg_price, median,
                mad, p_low, sample_count, updated_at)
            VALUES (:brand,:model,:year,:avg_price,:median,:mad,:p_low,
                :sample_count,:updated_at)
            ON CONFLICT(brand, model, year) DO UPDATE SET
                avg_price=excluded.avg_price, median=excluded.median,
                mad=excluded.mad, p_low=excluded.p_low,
                sample_count=excluded.sample_count,
                updated_at=excluded.updated_at
            """,
            stats,
        )
        return stats

    def get_model_price(
        self, brand: str, model: str, year: int
    ) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_prices WHERE brand = ? AND model = ? AND year = ?",
            (brand, model, year),
        ).fetchone()

    # -- seed state ---------------------------------------------------------
    def seeded_keys(self) -> set[str]:
        """Searches already seeded in a previous run — loaded once at startup so
        restarts don't re-fetch a corpus the db already holds."""
        return {r["seed_key"] for r in self.conn.execute(
            "SELECT seed_key FROM seed_state")}

    def mark_seeded(self, seed_key: str) -> None:
        self.conn.execute(
            "INSERT INTO seed_state (seed_key, seeded_at) VALUES (?,?) "
            "ON CONFLICT(seed_key) DO UPDATE SET seeded_at=excluded.seeded_at",
            (seed_key, time.time()),
        )
        self.conn.commit()

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
