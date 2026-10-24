import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeMenu
from zeroout import db, web
from zeroout.config import Settings
from zeroout.crawl import crawl
from zeroout.models import Observation, Option

YES = lambda a, b: True


def settings(tmp_path, cap=10.0):
    return Settings("", "", "", "", "", "", "", "", "", cap, "secret", audio_dir=str(tmp_path / "audio"))


def fake_tree():
    return {
        "": Observation("1 billing 2 agent", [Option("1", "billing"), Option("2", "agent")], "menu", 4.0),
        "1": Observation("please hold", [], "hold", 2.0),
        "2": Observation("a person", [], "human", 1.0),
    }


class FakeVerify:
    def __init__(self, ok=True):
        self.ok, self.started, self.checked = ok, [], []

    def start(self, e164):
        self.started.append(e164)

    def check(self, e164, code):
        self.checked.append((e164, code))
        return self.ok and code == "123456"


class FakeConnect:
    def __init__(self, exc=None):
        self.exc, self.calls, self.on_done = exc, [], None

    def __call__(self, conn, st, cid, path, phone, *, on_done=None, **kw):
        if self.exc:
            raise self.exc
        self.calls.append((cid, path, phone))
        self.on_done = on_done
        return 1


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.st = settings(tmp_path)
    e.conn = db.connect(":memory:")
    e.cid = db.add_company(e.conn, "Acme", "+18005550000")
    e.menu = FakeMenu(fake_tree())
    e.verify, e.connect = FakeVerify(), FakeConnect()
    e.answer = {"path": "2", "explanation": "Press 2 for an agent"}
    e.build = lambda st=None: web.create_app(
        st or e.st, e.conn, probe=e.menu.probe, listen=e.menu.listen, same=YES,
        chat=lambda s, m, msgs: e.answer, connect=e.connect, verify=e.verify,
    )
    e.app = e.build()
    e.client = TestClient(e.app)
    return e


def mapped(e):
    crawl(e.conn, e.st, e.cid, e.menu.probe, e.menu.listen, YES, sleep=lambda s: None)


def verified(e):
    e.client.post("/verify/start", data={"phone": "(415) 555-0100"})
    r = e.client.post("/verify/check", data={"phone": "(415) 555-0100", "code": "123456"})
    assert r.status_code == 200


def test_api_tree_is_nested_json(env):
    mapped(env)
    t = env.client.get(f"/api/companies/{env.cid}/tree").json()
    assert {c["key"]: c["label"] for c in t["children"]} == {"1": "billing", "2": "agent"}


def test_api_tree_unknown_404_and_unmapped_empty(env):
    assert env.client.get("/api/companies/99/tree").status_code == 404
    assert env.client.get(f"/api/companies/{env.cid}/tree").json() == {}


def test_add_non_toll_free_rejected(env):
    r = env.client.post("/companies", data={"name": "X", "number": "212-555-1234"})
    assert r.status_code == 400 and "toll-free" in r.text


def test_garbage_number_400_with_message(env):
    r = env.client.post("/companies", data={"name": "X", "number": "12345"})
    assert r.status_code == 400 and "Enter a US number like 1-800-555-1234" in r.text


def test_vanity_number_redirects(env):
    r = env.client.post("/companies", data={"name": "Flowers", "number": "1-800-FLOWERS"}, follow_redirects=False)
    assert r.status_code == 303
    row = env.conn.execute("SELECT * FROM companies WHERE number='+18003569377'").fetchone()
    assert r.headers["location"] == f"/c/{row['id']}"


def test_duplicate_redirects_to_existing(env):
    r = env.client.post("/companies", data={"name": "Again", "number": "800 555 0000"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/c/{env.cid}"
    assert env.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_connect_requires_verified_phone(env):
    r = env.client.post(f"/c/{env.cid}/connect", data={"path": "2"})
    assert r.status_code == 403 and not env.connect.calls


def test_verify_flow_then_connect(env):
    verified(env)
    assert env.verify.started == ["+14155550100"]
    assert env.verify.checked == [("+14155550100", "123456")]
    r = env.client.post(f"/c/{env.cid}/connect", data={"path": "2.1"}, follow_redirects=False)
    assert r.status_code < 400
    assert env.connect.calls == [(env.cid, "2.1", "+14155550100")]
    assert env.cid in env.app.state.running
    env.connect.on_done()
    assert env.cid not in env.app.state.running


def test_verify_bad_code_does_not_set_session(env):
    r = env.client.post("/verify/check", data={"phone": "4155550100", "code": "000000"})
    assert r.status_code == 400
    assert env.client.post(f"/c/{env.cid}/connect", data={"path": "2"}).status_code == 403


def test_verify_bad_phone_400(env):
    assert env.client.post("/verify/start", data={"phone": "123"}).status_code == 400


def test_connect_while_running_409(env):
    verified(env)
    env.app.state.running.add(env.cid)
    r = env.client.post(f"/c/{env.cid}/connect", data={"path": "2"})
    assert r.status_code == 409 and not env.connect.calls


def test_connect_value_error_400_and_lock_released(env):
    verified(env)
    env.connect.exc = ValueError("Path not mapped")
    r = env.client.post(f"/c/{env.cid}/connect", data={"path": "2"})
    assert r.status_code == 400 and "Path not mapped" in r.text
    assert env.cid not in env.app.state.running


def test_connect_unknown_company_404(env):
    verified(env)
    assert env.client.post("/c/99/connect", data={"path": "2"}).status_code == 404


def test_connect_rate_limited(env):
    verified(env)
    codes = []
    for _ in range(4):
        codes.append(env.client.post(f"/c/{env.cid}/connect", data={"path": "2"}).status_code)
        env.app.state.running.discard(env.cid)
    assert codes[3] == 429 and 429 not in codes[:3]


def test_second_crawl_same_company_conflicts(env):
    env.app.state.running.add(env.cid)
    r = env.client.post(f"/c/{env.cid}/crawl")
    assert r.status_code == 409 and "Already mapping this company" in r.text


def test_crawl_unknown_404(env):
    assert env.client.post("/c/99/crawl").status_code == 404


def test_crawl_runs_in_background_and_releases_lock(env):
    r = env.client.post(f"/c/{env.cid}/crawl", follow_redirects=False)
    assert r.status_code < 400
    assert db.get_node(env.conn, env.cid, "2")["status"] == "human"
    assert env.cid not in env.app.state.running


def test_crawl_releases_lock_on_error(env):
    def boom(*a, **k):
        raise RuntimeError("x")
    app = web.create_app(env.st, env.conn, probe=boom, listen=env.menu.listen, same=YES)
    c = TestClient(app, raise_server_exceptions=False)
    c.post(f"/c/{env.cid}/crawl")
    assert env.cid not in app.state.running


def test_company_page_renders_tree(env):
    mapped(env)
    r = env.client.get(f"/c/{env.cid}")
    assert r.status_code == 200 and "billing" in r.text and "<details" in r.text
    assert "/audio/" in r.text and f'/c/{env.cid}/connect' in r.text
    assert "Map this number" in r.text


def test_index_lists_companies(env):
    mapped(env)
    r = env.client.get("/")
    assert "Acme" in r.text and "+18005550000" in r.text


def test_timeline_lists_changes_newest_first(env):
    mapped(env)
    for path, ts in (("1", "2026-01-01T00:00:00"), ("2", "2026-02-01T00:00:00")):
        env.conn.execute(
            "INSERT INTO observations (company_id, path, raw_json, result, created_at) VALUES (?,?,?,?,?)",
            (env.cid, path, "{}", "changed", ts),
        )
    env.conn.commit()
    t = env.client.get(f"/c/{env.cid}").text
    assert t.index("2026-02-01") < t.index("2026-01-01")


def test_ask_not_mapped(env):
    r = env.client.post(f"/c/{env.cid}/ask", data={"question": "refund?"})
    assert r.status_code == 200 and "Not mapped yet" in r.text


def test_ask_shows_path_and_explanation(env):
    mapped(env)
    t = env.client.post(f"/c/{env.cid}/ask", data={"question": "agent"}).text
    assert "Press 2 for an agent" in t and "last confirmed" in t.lower()


def test_ask_multi_key_path_arrows(env):
    mapped(env)
    env.conn.execute("UPDATE nodes SET status='mapped' WHERE path='1'")
    env.conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '1.4', 'mapped')", (env.cid,))
    env.answer = {"path": "1.4", "explanation": "deep"}
    t = env.client.post(f"/c/{env.cid}/ask", data={"question": "q"}).text
    assert "1 → 4" in t


def test_ask_shows_stale_warning(env):
    mapped(env)
    env.conn.execute("UPDATE nodes SET status='stale' WHERE path='2'")
    env.conn.commit()
    t = env.client.post(f"/c/{env.cid}/ask", data={"question": "agent"}).text
    assert "stale" in t.lower() or "out of date" in t.lower()


def test_ask_unsure_shows_top_options(env):
    mapped(env)
    env.answer = {"path": None, "explanation": "?"}
    t = env.client.post(f"/c/{env.cid}/ask", data={"question": "zzz"}).text
    assert "billing" in t and "agent" in t


def test_ask_long_question_is_capped(env):
    mapped(env)
    seen = []
    app = web.create_app(env.st, env.conn, same=YES, chat=lambda s, m, msgs: seen.append(msgs) or env.answer)
    TestClient(app).post(f"/c/{env.cid}/ask", data={"question": "a" * 100000})
    assert len(seen[0][1]["content"]) < 20000


def test_ask_rate_limit_429_on_21st(env):
    mapped(env)
    codes = [env.client.post(f"/c/{env.cid}/ask", data={"question": "q"}).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_rate_limiter_window_with_clock():
    t = [0.0]
    rl = web.RateLimiter(2, 3600, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    t[0] = 3601
    assert rl.allow("a")


def test_audio_served(env, tmp_path):
    mapped(env)
    oid, url = env.conn.execute("SELECT id, audio_url FROM observations ORDER BY id LIMIT 1").fetchone()
    r = env.client.get(f"/audio/{oid}")
    assert r.status_code == 200 and r.content == open(url, "rb").read()


def test_audio_traversal_rejected(env, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    for url in (str(secret), str(tmp_path / "audio" / ".." / "secret.txt")):
        env.conn.execute(
            "INSERT INTO observations (company_id, path, raw_json, audio_url, created_at) VALUES (?,?,?,?,?)",
            (env.cid, "", "{}", url, "t"),
        )
        oid = env.conn.execute("SELECT MAX(id) FROM observations").fetchone()[0]
        assert env.client.get(f"/audio/{oid}").status_code == 404
    assert env.client.get("/audio/9999").status_code == 404


def test_spend_cap_503_and_banner(tmp_path, env):
    st = settings(tmp_path, cap=0)
    c = TestClient(env.build(st))
    r = c.post(f"/c/{env.cid}/crawl")
    assert r.status_code == 503 and "Daily call budget reached" in r.text
    assert c.get(f"/c/{env.cid}").text.count("Daily call budget reached") >= 1


def test_spend_cap_blocks_connect(tmp_path, env):
    c = TestClient(env.build(settings(tmp_path, cap=0)))
    c.post("/verify/check", data={"phone": "4155550100", "code": "123456"})
    assert c.post(f"/c/{env.cid}/connect", data={"path": "2"}).status_code == 503
    assert not env.connect.calls


def test_xss_company_name_escaped(env):
    db.add_company(env.conn, "<script>alert(1)</script>", "+18885550000")
    assert "<script>alert(1)" not in env.client.get("/").text
    assert "&lt;script&gt;" in env.client.get("/").text


def test_xss_prompt_text_escaped(env):
    env.conn.execute(
        "INSERT INTO nodes (company_id, path, prompt_text, options_json, status) VALUES (?,?,?,?,?)",
        (env.cid, "", "<img src=x onerror=alert(1)>", "[]", "mapped"),
    )
    env.conn.commit()
    assert "<img src=x" not in env.client.get(f"/c/{env.cid}").text


def test_import_has_no_side_effects():
    import importlib
    importlib.reload(web)
    assert "app" not in vars(web)
