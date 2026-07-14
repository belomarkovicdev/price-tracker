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
    raw          TEXT,
    first_seen   REAL,
    last_seen    REAL
);

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

-- Drop the retired market_stats table and bucket index from any pre-existing
-- db. The deal decision now reads model_prices only; nothing queried these.
DROP INDEX IF EXISTS idx_market_stats_model;
DROP TABLE IF EXISTS market_stats;
DROP INDEX IF EXISTS idx_listings_bucket;

-- One row per (site, brand, model, year, fuel) of price stats (median/MAD/
-- low-percentile + avg), collapsing mileage/gearbox variants into a single
-- record. The composite PRIMARY KEY guarantees we never duplicate a row for the
-- same group — repeat sightings update it in place. This is the only stats
-- table the deal decision reads.
--
-- `site` is in the key because prices are NOT comparable across markets (a
-- German Kleinanzeigen car and a Serbian polovniautomobili one price entirely
-- differently), and `fuel` because within a market diesel and petrol of the
-- same model-year sit at different price levels — comparing across fuel would
-- flag every petrol car as a "deal" against a diesel-inflated median.
CREATE TABLE IF NOT EXISTS model_prices (
    site         TEXT NOT NULL,
    brand        TEXT NOT NULL,
    model        TEXT NOT NULL,
    year         INTEGER NOT NULL,
    fuel         TEXT NOT NULL,
    avg_price    REAL,
    median       REAL,
    mad          REAL,
    p_low        REAL,
    sample_count INTEGER,
    updated_at   REAL,
    PRIMARY KEY (site, brand, model, year, fuel)
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
        try:
            contents = sorted(os.listdir(parent))[:20]
        except OSError:
            contents = "n/a"
        log.info(
            "Opening DB at %s (uid=%s, target exists=%s, target is_dir=%s, "
            "parent exists=%s, parent writable=%s, parent contents=%s)",
            db_path, uid, db_path.exists(), db_path.is_dir(),
            parent.exists(), os.access(parent, os.W_OK), contents,
        )
        # A common Railway mistake: the volume mount path was set to the DB file
        # path itself, so the mount point exists as a DIRECTORY and sqlite can't
        # open it as a file. Fail with a clear, actionable message.
        if db_path.is_dir():
            raise RuntimeError(
                f"{db_path} is a directory, not a file. The volume mount path "
                f"is almost certainly set to '{db_path}' — set it to '{parent}' "
                f"instead (the folder), keeping DB_PATH={db_path}. Or point "
                f"DB_PATH at a different filename inside the mounted folder."
            )
        try:
            self.conn = sqlite3.connect(str(db_path))
        except sqlite3.OperationalError as exc:
            log.error(
                "sqlite could not open %s: %s. Running as uid=%s; parent %s "
                "writable=%s. If on a mounted volume, set the volume mount path "
                "to the DB_PATH *directory* (%s), not the file.",
                db_path, exc, uid, parent, os.access(parent, os.W_OK), parent,
            )
            raise
        self.conn.row_factory = sqlite3.Row
        # WAL + NORMAL sync: writes stay durable per commit against app crashes,
        # fsyncs get far cheaper, and readers never block the writer. We commit
        # once per search-scan (batched by the engine) rather than per listing,
        # so the many upserts in a cycle cost one fsync instead of ~125.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Migrate any old model_prices before the schema recreates it (see below).
        self._migrate_model_prices()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._cleanup_polovni_rows()
        self._maybe_vacuum()

    def _maybe_vacuum(self) -> None:
        """Reclaim disk when much of the file is dead space. SQLite never shrinks
        the file on its own: emptied/deleted pages (e.g. the raw-blob overflow
        pages freed by the polovni scrub above) go onto an internal free list and
        are reused, but the file stays at its peak size until a VACUUM rebuilds
        it. VACUUM only when a meaningful fraction of the file is free so ordinary
        churn doesn't trigger a rewrite; once done the free list is ~empty, so
        this is a no-op on every subsequent open."""
        free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        total = self.conn.execute("PRAGMA page_count").fetchone()[0]
        if total and free > 1000 and free / total > 0.10:
            log.info(
                "Reclaiming disk space: %d of %d pages free — running VACUUM.",
                free, total,
            )
            self.conn.execute("VACUUM")
            # VACUUM can reset the journal mode; re-assert WAL to be safe.
            self.conn.execute("PRAGMA journal_mode=WAL")

    def _cleanup_polovni_rows(self) -> None:
        """One-time scrub of legacy polovni rows. Polovni now stores only
        brand/model/year/fuel/price (+ the structural id/url/title/status); rows
        written before that still hold the descriptive fields (mileage, gearbox,
        engine, power, city, image) and the full raw JSON blob we no longer keep.
        Null them out so the stored data matches what the scraper now writes.

        Idempotent: the WHERE clause matches only rows that still carry the old
        data, so after the first pass this is a cheap no-op each open. Scoped to
        polovniautomobili — kleinanzeigen still stores its (thin) raw + attrs."""
        cur = self.conn.execute(
            "UPDATE listings SET raw = '{}', mileage = NULL, gearbox = NULL, "
            "engine_cc = NULL, power_kw = NULL, city = NULL, image = NULL "
            "WHERE site = 'polovniautomobili' AND ("
            "  raw IS NOT NULL AND raw != '{}' OR mileage IS NOT NULL "
            "  OR gearbox IS NOT NULL OR engine_cc IS NOT NULL "
            "  OR power_kw IS NOT NULL OR city IS NOT NULL OR image IS NOT NULL)"
        )
        if cur.rowcount:
            log.info(
                "Scrubbed %d legacy polovni row(s) to the stored fields "
                "(dropped raw blob + mileage/gearbox/engine/power/city/image).",
                cur.rowcount,
            )
        self.conn.commit()

    def _migrate_model_prices(self) -> None:
        """model_prices is now keyed by (site, brand, model, year, fuel). Older
        dbs keyed it by (brand, model, year) only. SQLite can't ALTER a primary
        key, but this table is a derived cache (recomputed from `listings` on the
        next scan), so the safe migration is simply to drop the stale-schema
        table and let _SCHEMA recreate it with the new key."""
        cols = {r["name"]
                for r in self.conn.execute("PRAGMA table_info(model_prices)")}
        if cols and "site" not in cols:
            log.info(
                "Migrating model_prices to (site, brand, model, year, fuel) "
                "schema — dropping the stale cache; it recomputes on next scan."
            )
            self.conn.execute("DROP TABLE model_prices")

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
                engine_cc, power_kw, city, status, featured, image, raw,
                first_seen, last_seen)
            VALUES (:key,:site,:listing_id,:search_name,:url,:title,:price,
                :currency,:brand,:model,:year,:mileage,:fuel,:gearbox,:engine_cc,
                :power_kw,:city,:status,:featured,:image,:raw,:now,:now)
            ON CONFLICT(key) DO UPDATE SET
                search_name=excluded.search_name, url=excluded.url,
                title=excluded.title, price=excluded.price,
                status=excluded.status, featured=excluded.featured,
                image=excluded.image,
                mileage=excluded.mileage, last_seen=:now,
                -- Refresh/backfill the structured comparables, but never wipe a
                -- stored value with an incoming NULL: polovni always carries
                -- these on the list page (so they stay current), while
                -- kleinanzeigen reuses once-fetched detail attrs and must keep
                -- them if a later scan lacks them. COALESCE = keep old on NULL.
                fuel=COALESCE(excluded.fuel, fuel),
                brand=COALESCE(excluded.brand, brand),
                model=COALESCE(excluded.model, model),
                year=COALESCE(excluded.year, year),
                gearbox=COALESCE(excluded.gearbox, gearbox),
                engine_cc=COALESCE(excluded.engine_cc, engine_cc),
                power_kw=COALESCE(excluded.power_kw, power_kw),
                city=COALESCE(excluded.city, city)
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
                "image": listing.image,
                "raw": json.dumps(listing.raw, ensure_ascii=False), "now": now,
            },
        )

        if is_new or (prev_price != listing.price):
            self.conn.execute(
                "INSERT INTO price_history (key, price, seen_at) VALUES (?,?,?)",
                (listing.key, listing.price, now),
            )
        return is_new, prev_price

    # -- per (site, model, year, fuel) average -----------------------------
    def update_model_price(
        self, site: str, brand: str, model: str, year: int, fuel: str,
        bottom_percentile: float = 20.0,
        min_rows: int = 1,
    ) -> Optional[dict]:
        """Recompute the price stats (avg, median, MAD, low-percentile) across
        all active, priced listings with this exact site+brand+model+year+fuel
        and upsert the single row for it. Returns the row (or None if there are
        no priced listings for it yet).

        Scoping to `site` keeps markets from being mixed (see the schema note),
        and to `fuel` keeps diesel and petrol of the same model-year in separate
        pools. The whole sample is read here — this is the one place that needs
        it — so the stored median/MAD are as robust as a live computation.
        Evaluation then reads just this one row.

        This always recomputes from the full sample; callers decide *when* to
        call it. The engine computes a row the first time a group is seen (so it
        can be judged that same cycle), and the hourly maintenance pass
        force-refreshes every group seen in the last hour.

        `min_rows` skips the group unless it has at least that many priced,
        active comparables (returns None without touching the stored row) — the
        hourly pass uses it to only update groups with more than 4 rows."""
        prices = [r["price"] for r in self.conn.execute(
            "SELECT price FROM listings "
            "WHERE site = ? AND brand = ? AND model = ? AND year = ? "
            "AND fuel = ? AND price IS NOT NULL AND status = 'active'",
            (site, brand, model, year, fuel),
        )]
        if len(prices) < min_rows:
            return None
        median = statistics.median(prices)
        stats = {
            "site": site, "brand": brand, "model": model, "year": year,
            "fuel": fuel,
            "avg_price": statistics.fmean(prices),
            "median": median,
            "mad": statistics.median([abs(p - median) for p in prices]),
            "p_low": _percentile(sorted(prices), bottom_percentile),
            "sample_count": len(prices),
            "updated_at": time.time(),
        }
        self.conn.execute(
            """
            INSERT INTO model_prices (site, brand, model, year, fuel, avg_price,
                median, mad, p_low, sample_count, updated_at)
            VALUES (:site,:brand,:model,:year,:fuel,:avg_price,:median,:mad,
                :p_low,:sample_count,:updated_at)
            ON CONFLICT(site, brand, model, year, fuel) DO UPDATE SET
                avg_price=excluded.avg_price, median=excluded.median,
                mad=excluded.mad, p_low=excluded.p_low,
                sample_count=excluded.sample_count,
                updated_at=excluded.updated_at
            """,
            stats,
        )
        return stats

    def refresh_medians_since(
        self, since_ts: float, bottom_percentile: float = 20.0,
        min_rows: int = 1,
    ) -> tuple[int, int]:
        """Force-recompute the stored price stats (median/MAD/low-percentile) for
        every group that received a listing since `since_ts`, and return
        (groups_updated, listings_covered).

        The time window only selects WHICH groups to consider — the ones whose
        data changed in the last hour. Each group's median is still computed over
        its FULL active corpus (via update_model_price, which recomputes from the
        whole sample), because an hour of listings alone is too thin to be a
        robust median. `min_rows` is passed straight through, so groups with
        fewer than that many comparables are skipped (not updated). Does not
        commit — the caller does."""
        candidates = self.conn.execute(
            "SELECT DISTINCT site, brand, model, year, fuel FROM listings "
            "WHERE last_seen >= ? AND price IS NOT NULL AND status = 'active' "
            "AND brand IS NOT NULL AND model IS NOT NULL "
            "AND year IS NOT NULL AND fuel IS NOT NULL",
            (since_ts,),
        ).fetchall()
        updated = listings = 0
        for g in candidates:
            stats = self.update_model_price(
                g["site"], g["brand"], g["model"], g["year"], g["fuel"],
                bottom_percentile=bottom_percentile, min_rows=min_rows,
            )
            if stats is not None:
                updated += 1
                listings += stats["sample_count"]
        return updated, listings

    def get_model_price(
        self, site: str, brand: str, model: str, year: int, fuel: str
    ) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_prices WHERE site = ? AND brand = ? "
            "AND model = ? AND year = ? AND fuel = ?",
            (site, brand, model, year, fuel),
        ).fetchone()

    def get_listing_attrs(self, key: str) -> Optional[dict]:
        """The structured car attributes we already stored for a listing, or
        None if we've never seen it. Lets a scraper reuse a known ad's details
        (e.g. from an earlier detail-page fetch) instead of fetching them again."""
        row = self.conn.execute(
            "SELECT brand, model, year, fuel, mileage, gearbox, power_kw, "
            "engine_cc, city FROM listings WHERE key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row is not None else None

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
