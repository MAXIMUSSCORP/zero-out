from zeroout.db import add_company, connect, get_node


def test_connect_creates_tables():
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "nodes", "observations", "runs"} <= names


def test_add_company_and_get_missing_node():
    conn = connect(":memory:")
    assert add_company(conn, "Acme", "+18005551234") == 1
    assert get_node(conn, 1, "") is None
