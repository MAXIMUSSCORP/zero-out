import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .db import LOCK


@dataclass
class Budget:
    max_calls: int = 40
    max_minutes: float = 30
    max_depth: int = 4


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def can_spend(conn: sqlite3.Connection, settings: Settings) -> bool:
    with LOCK:
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE substr(started_at, 1, 10) = ?",
            (_today(),),
        ).fetchone()[0]
    return spent < settings.daily_cap_usd


def calls_today(conn: sqlite3.Connection, company_id: int) -> tuple[int, float]:
    """Crawl calls and minutes already used on this company today (UTC)."""
    with LOCK:
        row = conn.execute(
            "SELECT COALESCE(SUM(calls), 0), COALESCE(SUM(minutes), 0) FROM runs "
            "WHERE company_id = ? AND kind = 'crawl' AND substr(started_at, 1, 10) = ?",
            (company_id, _today()),
        ).fetchone()
    return row[0], row[1]
