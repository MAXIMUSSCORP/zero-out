import os
import sqlite3
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
    kind TEXT NOT NULL CHECK (kind IN ('crawl', 'connect')),
    contributor_handle TEXT,
    calls INTEGER NOT NULL DEFAULT 0,
    minutes REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # The web app hands this connection to background tasks on other threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_company(conn: sqlite3.Connection, name: str, number: str) -> int:
    cur = conn.execute(
        "INSERT INTO companies (name, number) VALUES (?, ?)", (name, number)
    )
    conn.commit()
    return cur.lastrowid


def get_node(conn: sqlite3.Connection, company_id: int, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM nodes WHERE company_id = ? AND path = ?", (company_id, path)
    ).fetchone()


def start_run(conn: sqlite3.Connection, company_id: int, kind: str, contributor: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (company_id, kind, contributor_handle, started_at) VALUES (?, ?, ?, ?)",
        (company_id, kind, contributor, now()),
    )
    conn.commit()
    return cur.lastrowid
