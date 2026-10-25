from zeroout.db import add_company, connect, get_node


def test_connect_creates_tables():
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "nodes", "observations", "runs"} <= names


def test_add_company_and_get_missing_node():
    conn = connect(":memory:")
    assert add_company(conn, "Acme", "+18005551234") == 1
    assert get_node(conn, 1, "") is None


def test_calls_today_counts_only_todays_crawls_for_company():
    from zeroout.budget import calls_today
    conn = connect(":memory:")
    a, b = add_company(conn, "A", "+18005550001"), add_company(conn, "B", "+18005550002")
    conn.execute("INSERT INTO runs (company_id, kind, calls, minutes, started_at) VALUES (?, 'crawl', 5, 2.5, datetime('now'))", (a,))
    conn.execute("INSERT INTO runs (company_id, kind, calls, minutes, started_at) VALUES (?, 'crawl', 7, 1, '2000-01-01T00:00:00')", (a,))
    conn.execute("INSERT INTO runs (company_id, kind, calls, minutes, started_at) VALUES (?, 'connect', 2, 1, datetime('now'))", (a,))
    conn.execute("INSERT INTO runs (company_id, kind, calls, minutes, started_at) VALUES (?, 'crawl', 9, 9, datetime('now'))", (b,))
    assert calls_today(conn, a) == (5, 2.5)


OLD_SCHEMA = """
CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, number TEXT NOT NULL UNIQUE);
CREATE TABLE runs (
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
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    raw_json TEXT NOT NULL,
    audio_url TEXT,
    result TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO companies (id, name, number) VALUES (1, 'Acme', '+18005551234');
INSERT INTO runs (id, company_id, kind, contributor_handle, calls, minutes, cost_usd, started_at, ended_at)
    VALUES (4, 1, 'crawl', 'zed', 3, 1.5, 0.25, '2026-10-01T00:00:00', '2026-10-01T00:05:00');
INSERT INTO runs (id, company_id, kind, started_at) VALUES (9, 1, 'connect', '2026-10-02T00:00:00');
INSERT INTO observations (id, company_id, path, run_id, raw_json, audio_url, result, created_at)
    VALUES (7, 1, '1', 4, '{}', '/a/1.wav', 'mapped', '2026-10-01T00:01:00');
"""


def runs_sql(conn):
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0]


def make_old_db(path):
    import sqlite3
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()


def test_old_runs_check_is_rebuilt_keeping_rows_and_ids(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    conn = connect(path)
    rows = [tuple(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]
    assert rows == [
        (4, 1, "crawl", "zed", 3, 1.5, 0.25, "2026-10-01T00:00:00", "2026-10-01T00:05:00"),
        (9, 1, "connect", None, 0, 0.0, 0.0, "2026-10-02T00:00:00", None),
    ]
    assert tuple(conn.execute("SELECT id, run_id FROM observations").fetchone()) == (7, 4)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert "runs_new" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "REFERENCES runs(id)" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'observations'").fetchone()[0]
    cur = conn.execute("INSERT INTO runs (company_id, kind, started_at) VALUES (1, 'phone', 'x')")
    assert cur.lastrowid == 10  # autoincrement carried on past the old ids


def test_second_connect_is_a_no_op(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    first = connect(path)
    sql = runs_sql(first)
    first.close()
    second = connect(path)
    assert runs_sql(second) == sql
    assert second.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_empty_old_db_migrates(tmp_path):
    import sqlite3
    path = str(tmp_path / "empty.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA.split("INSERT")[0])
    raw.close()
    conn = connect(path)
    assert "'phone'" in runs_sql(conn)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_fresh_db_accepts_phone_and_refuses_bogus():
    import sqlite3
    import pytest
    conn = connect(":memory:")
    assert "'phone'" in runs_sql(conn)
    cid = add_company(conn, "A", "+18005550001")
    conn.execute("INSERT INTO runs (company_id, kind, started_at) VALUES (?, 'phone', 'x')", (cid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO runs (company_id, kind, started_at) VALUES (?, 'bogus', 'x')", (cid,))


def test_migrated_db_still_refuses_bogus(tmp_path):
    import sqlite3
    import pytest
    path = str(tmp_path / "old.db")
    make_old_db(path)
    conn = connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO runs (company_id, kind, started_at) VALUES (1, 'bogus', 'x')")


def test_old_orphan_does_not_block_startup_and_foreign_keys_are_restored(tmp_path):
    import sqlite3
    path = str(tmp_path / "bad.db")
    make_old_db(path)
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO observations (company_id, path, run_id, raw_json, created_at) "
                "VALUES (1, '2', 999, '{}', 'x')")
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "'phone'" in runs_sql(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()


def test_migration_undone_when_rebuild_adds_orphans(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    from zeroout import db
    path = str(tmp_path / "old.db")
    make_old_db(path)
    counts = iter([0, 1])
    monkeypatch.setattr(db, "_orphans", lambda conn: next(counts))
    with pytest.raises(RuntimeError, match="foreign key"):
        connect(path)
    check = sqlite3.connect(path)
    assert "'phone'" not in check.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
    assert check.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_foreign_keys_setting_is_restored_after_rebuild(tmp_path):
    import sqlite3
    from zeroout import db
    path = str(tmp_path / "old.db")
    make_old_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._migrate_runs_kind(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert "'phone'" in runs_sql(conn)


def test_nodes_source_column_is_added_to_an_old_db_once(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA + """
CREATE TABLE nodes (company_id INTEGER NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unexplored',
    PRIMARY KEY (company_id, path));
INSERT INTO nodes (company_id, path, status) VALUES (1, '', 'mapped');
""")
    raw.commit()
    raw.close()
    for _ in range(2):
        conn = connect(path)
        assert conn.execute("SELECT source FROM nodes").fetchall()[0][0] == "crawl"
        conn.close()


def test_fresh_nodes_default_source_is_crawl():
    conn = connect(":memory:")
    cid = add_company(conn, "A", "+18005550001")
    conn.execute("INSERT INTO nodes (company_id, path) VALUES (?, '')", (cid,))
    assert get_node(conn, cid, "")["source"] == "crawl"


def test_orphan_in_another_table_does_not_stop_startup(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    make_old_db(path)
    raw = sqlite3.connect(path)
    raw.executescript("""
CREATE TABLE nodes (company_id INTEGER NOT NULL REFERENCES companies(id), path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unexplored', PRIMARY KEY (company_id, path));
INSERT INTO nodes (company_id, path) VALUES (77, '');
""")
    raw.commit()
    assert len(raw.execute("PRAGMA foreign_key_check").fetchall()) == 1  # the orphan is real
    raw.close()
    conn = connect(path)
    assert "'phone'" in runs_sql(conn)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1


def test_migration_reads_the_runs_sql_again_once_it_holds_the_write_lock(tmp_path, monkeypatch):
    """Another process may rebuild the table between the first look and the lock; here a second
    connection does, and the first must find it done and leave the table alone."""
    import sqlite3
    from zeroout import db
    path = str(tmp_path / "old.db")
    make_old_db(path)
    real = db._runs_sql
    calls = []

    def racing(conn):
        sql = real(conn)
        calls.append(sql)
        if len(calls) == 1:
            other = sqlite3.connect(path)
            other.row_factory = sqlite3.Row
            db._migrate_runs_kind(other)
            other.close()
        return sql

    monkeypatch.setattr(db, "_runs_sql", racing)
    conn = connect(path)
    # the first look (stale), the other connection's two looks, then this one's look under the lock
    assert len(calls) == 4 and "'phone'" not in calls[0] and "'phone'" in calls[3]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert "runs_new" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


def test_orphan_count_is_scoped_to_observations(tmp_path):
    import sqlite3
    from zeroout import db
    conn = connect(":memory:")
    add_company(conn, "A", "+18005550001")
    conn.execute("INSERT INTO nodes (company_id, path) VALUES (77, '')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() != []
    assert db._orphans(conn) == 0
    conn.execute("INSERT INTO observations (company_id, path, run_id, raw_json, created_at) "
                 "VALUES (1, '', 999, '{}', 'x')")
    assert db._orphans(conn) == 1
