"""Load config.yaml and .env into simple typed objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Does not override existing
    environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class SearchConfig:
    name: str
    url: str


@dataclass
class SiteConfig:
    name: str
    enabled: bool
    request_delay_seconds: float
    jitter_seconds: float
    skip_pages: int          # leading pages to never fetch (paid "members-first")
    seed_pages: int          # regular pages to fetch on first sight (build corpus)
    scan_pages: int          # regular pages to fetch each steady-state cycle
    seed_enabled: bool       # if False, never seed — only ever the steady-state
                             # scan (watch for new posts, build the corpus slowly)
    searches: list[SearchConfig]
    max_detail_fetches_per_cycle: int = 0
                             # cap detail-page fetches per cycle so a burst of new
                             # ads doesn't fire a rapid run of requests. 0 =
                             # unlimited. Over-budget new ads defer to next cycle.

    @property
    def start_page(self) -> int:
        """First page we ever fetch — right after the paid block."""
        return self.skip_pages + 1


@dataclass
class EvaluatorConfig:
    min_samples: int = 8
    mad_k: float = 1.5
    bottom_percentile: float = 20.0
    min_deal_discount: float = 0.12
    scam_floor_ratio: float = 0.35
    min_price: float = 300.0


@dataclass
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)


@dataclass
class Config:
    poll_interval_seconds: float
    evaluator: EvaluatorConfig
    telegram: TelegramConfig
    sites: list[SiteConfig]
    # How often the engine force-refreshes the stored market medians (and posts a
    # Telegram "DB is being updated" heartbeat) for groups seen in that window.
    # Default hourly. 0 disables the periodic refresh (per-scan 24h TTL remains).
    median_refresh_interval_seconds: float = 3600.0
    db_path: Path = field(default=ROOT / "price_tracker.db")


def load_config(path: Path | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    path = path or (ROOT / "config.yaml")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    ev = data.get("evaluator", {}) or {}
    evaluator = EvaluatorConfig(
        min_samples=int(ev.get("min_samples", 8)),
        mad_k=float(ev.get("mad_k", 1.5)),
        bottom_percentile=float(ev.get("bottom_percentile", 20.0)),
        min_deal_discount=float(ev.get("min_deal_discount", 0.12)),
        scam_floor_ratio=float(ev.get("scam_floor_ratio", 0.35)),
        min_price=float(ev.get("min_price", 300.0)),
    )

    tg = (data.get("notify", {}) or {}).get("telegram", {}) or {}
    telegram = TelegramConfig(
        enabled=bool(tg.get("enabled", True)),
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )

    sites: list[SiteConfig] = []
    for name, cfg in (data.get("sites", {}) or {}).items():
        cfg = cfg or {}
        searches = [
            SearchConfig(name=s["name"], url=s["url"])
            for s in (cfg.get("searches", []) or [])
        ]
        sites.append(
            SiteConfig(
                name=name,
                enabled=bool(cfg.get("enabled", True)),
                request_delay_seconds=float(cfg.get("request_delay_seconds", 6)),
                jitter_seconds=float(cfg.get("jitter_seconds", 4)),
                skip_pages=max(0, int(cfg.get("skip_pages", 5))),
                seed_pages=max(1, int(cfg.get("seed_pages", 5))),
                scan_pages=max(1, int(cfg.get("scan_pages", 2))),
                seed_enabled=bool(cfg.get("seed_enabled", True)),
                searches=searches,
                max_detail_fetches_per_cycle=max(
                    0, int(cfg.get("max_detail_fetches_per_cycle", 0))),
            )
        )

    # DB location: default next to the code, or override with DB_PATH so a
    # mounted volume (e.g. Railway) can hold the corpus across redeploys.
    db_env = os.environ.get("DB_PATH")
    db_path = Path(db_env) if db_env else ROOT / "price_tracker.db"

    return Config(
        poll_interval_seconds=float(data.get("poll_interval_seconds", 20)),
        evaluator=evaluator,
        telegram=telegram,
        sites=sites,
        median_refresh_interval_seconds=float(
            data.get("median_refresh_interval_seconds", 3600)),
        db_path=db_path,
    )
