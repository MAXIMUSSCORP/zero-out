import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    number TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS nodes (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    path TEXT NOT NULL,
    prompt_text TEXT,
    options_json TEXT,
    outcome TEXT,
    prompt_seconds REAL,
    status TEXT NOT NULL DEFAULT 'unexplored',
    confirmations INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_confirmed TEXT,
    timing_correction REAL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'crawl',
    PRIMARY KEY (company_id, path)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    raw_json TEXT NOT NULL,
    audio_url TEXT,
    result TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    kind TEXT NOT NULL CHECK (kind IN ('crawl', 'connect', 'phone')),
    contributor_handle TEXT,
    calls INTEGER NOT NULL DEFAULT 0,
    minutes REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
"""


# One connection is shared by request handlers and background calls; hold this
# around every transaction (and multi-statement read) on it.
LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # The web app hands this connection to background tasks on other threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_node_source(conn)
    _migrate_runs_kind(conn)
    return conn


RUNS_COLUMNS = (
    "id, company_id, kind, contributor_handle, calls, minutes, cost_usd, started_at, ended_at"
)


def _add_node_source(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "source" not in cols:
        with LOCK:
            conn.execute("ALTER TABLE nodes ADD COLUMN source TEXT NOT NULL DEFAULT 'crawl'")
            conn.commit()


def _orphans(conn: sqlite3.Connection) -> int:
    return len(conn.execute("PRAGMA foreign_key_check(observations)").fetchall())


def _runs_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
    return row[0] if row else None


def _migrate_runs_kind(conn: sqlite3.Connection) -> None:
    """SQLite cannot alter a CHECK, so an older runs table is rebuilt to allow kind 'phone'."""
    sql = _runs_sql(conn)
    if sql is None or "'phone'" in sql:
        return
    with LOCK:
        conn.commit()
        was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        # Off, so DROP TABLE does not run the implicit delete that would act on observations.run_id.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            sql = _runs_sql(conn)  # read again under the write lock
            if sql is None or "'phone'" in sql:
                conn.rollback()
                return
            new_sql = sql.replace("'connect')", "'connect', 'phone')")
            if new_sql == sql:
                raise RuntimeError("The runs table has a kind check this version does not recognise.")
            new_sql = new_sql.replace("runs", "runs_new", 1)
            before = _orphans(conn)  # old orphans must not stop the app from starting
            conn.execute(new_sql)
            conn.execute(f"INSERT INTO runs_new ({RUNS_COLUMNS}) SELECT {RUNS_COLUMNS} FROM runs")
            conn.execute("DROP TABLE runs")
            conn.execute("ALTER TABLE runs_new RENAME TO runs")
            if _orphans(conn) > before:
                raise RuntimeError("The runs table rebuild left a foreign key broken, so it was undone.")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute(f"PRAGMA foreign_keys = {'ON' if was_on else 'OFF'}")


def add_company(conn: sqlite3.Connection, name: str, number: str) -> int:
    with LOCK:
        cur = conn.execute(
            "INSERT INTO companies (name, number) VALUES (?, ?)", (name, number)
        )
        conn.commit()
    return cur.lastrowid


def get_node(conn: sqlite3.Connection, company_id: int, path: str) -> sqlite3.Row | None:
    with LOCK:
        return conn.execute(
            "SELECT * FROM nodes WHERE company_id = ? AND path = ?", (company_id, path)
        ).fetchone()


def start_run(conn: sqlite3.Connection, company_id: int, kind: str, contributor: str) -> int:
    with LOCK:
        cur = conn.execute(
            "INSERT INTO runs (company_id, kind, contributor_handle, started_at) VALUES (?, ?, ?, ?)",
            (company_id, kind, contributor, now()),
        )
        conn.commit()
    return cur.lastrowid
