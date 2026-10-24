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
