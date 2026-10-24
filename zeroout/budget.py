import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings


@dataclass
class Budget:
    max_calls: int = 40
    max_minutes: float = 30
    max_depth: int = 4


def can_spend(conn: sqlite3.Connection, settings: Settings) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    spent = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE substr(started_at, 1, 10) = ?",
        (today,),
    ).fetchone()[0]
    return spent < settings.daily_cap_usd
