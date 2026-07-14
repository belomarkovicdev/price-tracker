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

import logging
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# The site whose data lived in the old single-file db (price_tracker.db). When we
# switch to one db per site, that legacy file is adopted as this site's db.
_LEGACY_DB_SITE = "polovniautomobili"
_LEGACY_DB_NAME = "price_tracker.db"


def open_site_store(db_dir: Path, site: str) -> "Store":
    """Open the per-site db for `site` inside the volume `db_dir` (creating the
    file if needed). Each site gets its own file — polovniautomobili.db,
    kleinanzeigen.db, … — so adding a site never touches another's data, and all
    live in one mounted volume.

    Migration: the first time we open the polovni db and the old shared
    price_tracker.db still exists, adopt it (move the .db + its -wal/-shm) instead
    of starting empty, so the accumulated corpus carries over. Any non-polovni
    rows it still holds are purged by the Store's foreign-site cleanup."""
    db_dir = Path(db_dir)
    target = db_dir / f"{site}.db"
    legacy = db_dir / _LEGACY_DB_NAME
    if site == _LEGACY_DB_SITE and not target.exists() and legacy.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(legacy) + suffix)
            if src.exists():
                os.replace(src, Path(str(target) + suffix))
        log.info("Adopted legacy %s as %s.", _LEGACY_DB_NAME, target.name)
    return Store(target, site=site)


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
-- The db now stores ONLY the aggregate medians and the alert-dedup log.
-- Individual listings are NOT persisted: they live in an in-memory rolling
-- buffer (see buffer.ListingBuffer) from which medians are computed. Drop the
-- old per-listing tables from any pre-existing db and reclaim their space.
DROP TABLE IF EXISTS listings;
DROP INDEX IF EXISTS idx_listings_bucket;
DROP TABLE IF EXISTS price_history;
DROP INDEX IF EXISTS idx_price_history_key;
DROP TABLE IF EXISTS seed_state;
DROP INDEX IF EXISTS idx_market_stats_model;
DROP TABLE IF EXISTS market_stats;

-- One row per (site, brand, model, year, fuel) of price stats (median/MAD/
-- low-percentile + avg). Rebuilt from the in-memory buffer on each refresh, so
-- it always reflects the current rolling window. This is the durable output —
-- the price-median database you query.
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

-- Alert-dedup log, so we don't re-alert the same ad (persisted across restarts
-- and cycles). A later price drop re-qualifies it.
CREATE TABLE IF NOT EXISTS alerts (
    key           TEXT PRIMARY KEY,
    alerted_price REAL,
    alerted_at    REAL
);
"""


def price_stats(prices: list[float], bottom_percentile: float = 20.0) -> dict:
    """Median/MAD/low-percentile/avg over a non-empty price list — the one place
    the stats are defined, shared by the in-memory evaluation and the persisted
    model_prices rebuild."""
    median = statistics.median(prices)
    return {
        "avg_price": statistics.fmean(prices),
        "median": median,
        "mad": statistics.median([abs(p - median) for p in prices]),
        "p_low": _percentile(sorted(prices), bottom_percentile),
        "sample_count": len(prices),
    }


class Store:
    def __init__(self, db_path: Path, site: Optional[str] = None) -> None:
        # `site`, when given, is the one site this db belongs to; rows from any
        # other site are purged on open so a per-site db stays single-site.
        self.site = site
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
        if self.site is not None:
            self._drop_foreign_site_rows(self.site)
        self._maybe_vacuum()

    def _drop_foreign_site_rows(self, own_site: str) -> None:
        """Enforce the one-db-per-site invariant: delete every row that doesn't
        belong to `own_site`. Purges kleinanzeigen (and anything else) out of the
        polovni db adopted from the old shared price_tracker.db. Idempotent:
        matches nothing once the db holds only its own site."""
        n = self.conn.execute(
            "DELETE FROM model_prices WHERE site != ?", (own_site,)).rowcount
        # alerts key on "<site>:<id>".
        self.conn.execute(
            "DELETE FROM alerts WHERE key NOT LIKE ?", (own_site + ":%",))
        if n:
            log.info(
                "Purged %d model_prices row(s) from other sites — this is the "
                "%s database.", n, own_site,
            )
        self.conn.commit()

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

    def _migrate_model_prices(self) -> None:
        """model_prices is now keyed by (site, brand, model, year, fuel). Older
        dbs keyed it by (brand, model, year) only. SQLite can't ALTER a primary
        key, but this table is a derived cache (rebuilt from the in-memory buffer
        on the next refresh), so the safe migration is simply to drop the
        stale-schema table and let _SCHEMA recreate it with the new key."""
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

    # -- medians (rebuilt from the in-memory buffer) ------------------------
    def rebuild_model_prices(
        self, group_prices: dict, bottom_percentile: float = 20.0,
        min_rows: int = 1,
    ) -> tuple[int, int]:
        """Replace this site's stored medians with a fresh set computed from
        `group_prices` — a {(brand, model, year, fuel): [price, …]} snapshot of
        the in-memory buffer. Groups with fewer than `min_rows` prices are left
        out entirely (no stale rows survive). Returns (groups_written,
        prices_used). Does not commit — the caller does.

        We delete-then-insert the whole site so model_prices is always an exact
        projection of the current rolling window: a group that aged out simply
        stops having a row."""
        self.conn.execute(
            "DELETE FROM model_prices WHERE site = ?", (self.site,))
        now = time.time()
        groups = used = 0
        for (brand, model, year, fuel), prices in group_prices.items():
            if len(prices) < min_rows:
                continue
            s = price_stats(prices, bottom_percentile)
            self.conn.execute(
                """
                INSERT INTO model_prices (site, brand, model, year, fuel,
                    avg_price, median, mad, p_low, sample_count, updated_at)
                VALUES (:site,:brand,:model,:year,:fuel,:avg_price,:median,:mad,
                    :p_low,:sample_count,:updated_at)
                """,
                {"site": self.site, "brand": brand, "model": model,
                 "year": year, "fuel": fuel, "updated_at": now, **s},
            )
            groups += 1
            used += len(prices)
        return groups, used

    def get_model_price(
        self, site: str, brand: str, model: str, year: int, fuel: str
    ) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_prices WHERE site = ? AND brand = ? "
            "AND model = ? AND year = ? AND fuel = ?",
            (site, brand, model, year, fuel),
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
