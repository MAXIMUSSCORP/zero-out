import re
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeMenu
from zeroout import db, web
from zeroout.config import Settings
from zeroout.crawl import crawl
from zeroout.models import Observation, Option

YES = lambda a, b: True
SECRET = "s" * 48
HX = {"HX-Request": "true"}
EVIL = {"Origin": "http://evil.example"}


def settings(tmp_path, cap=10.0):
    return Settings("", "", "", "", "", "", "", "", "", cap, SECRET,
                    audio_dir=str(tmp_path / "audio"), cookie_secure=False)


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


# --- security fixes ---------------------------------------------------------


@pytest.mark.parametrize("secret", ["", "short", "change-me", "x" * 31])
def test_weak_session_secret_refused(env, secret):
    st = settings(env.st.audio_dir and __import__("pathlib").Path(env.st.audio_dir).parent)
    st.session_secret = secret
    with pytest.raises(RuntimeError):
        web.create_app(st, env.conn)


def test_session_cookie_flags(env):
    st = settings(__import__("pathlib").Path(env.st.audio_dir).parent)
    st.cookie_secure = True
    c = TestClient(env.build(st), base_url="https://testserver")
    c.post("/verify/check", data={"phone": "(415) 555-0100", "code": "123456"})
    cookie = c.post("/verify/check", data={"phone": "(415) 555-0100", "code": "123456"}).headers["set-cookie"].lower()
    assert "secure" in cookie and "samesite=lax" in cookie and "httponly" in cookie and "max-age=3600" in cookie


def test_cookie_secure_setting_from_env(monkeypatch):
    from zeroout.config import load_settings
    monkeypatch.setattr("zeroout.config.load_dotenv", lambda: None)
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    assert load_settings().cookie_secure is True
    for v, want in (("0", False), ("false", False), ("1", True)):
        monkeypatch.setenv("COOKIE_SECURE", v)
        assert load_settings().cookie_secure is want


def test_cross_origin_post_rejected_on_verify_and_connect(env):
    verified(env)
    for url, data in (
        ("/verify/start", {"phone": "4155550100"}),
        ("/verify/check", {"phone": "4155550100", "code": "123456"}),
        (f"/c/{env.cid}/connect", {"path": "2"}),
    ):
        assert env.client.post(url, data=data, headers=EVIL).status_code == 403
    assert not env.connect.calls and env.verify.started == ["+14155550100"]


def test_same_origin_post_allowed(env):
    r = env.client.post("/verify/start", data={"phone": "4155550100"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


@pytest.mark.parametrize("phone", ["416-555-0100", "(876) 555-0100", "787 555 0100", "671-555-0100", "900-555-0100", "976-555-0100"])
def test_verify_start_rejects_non_us_and_premium(env, phone):
    r = env.client.post("/verify/start", data={"phone": phone})
    assert r.status_code == 400 and not env.verify.started


def test_verify_start_per_phone_limit(env):
    codes = []
    for i in range(4):
        codes.append(env.client.post("/verify/start", data={"phone": "4155550100"}).status_code)
    assert codes == [200, 200, 200, 429]
    assert len(env.verify.started) == 3


def test_verify_start_per_phone_limit_is_per_phone(env):
    for _ in range(3):
        env.client.post("/verify/start", data={"phone": "4155550100"})
    assert env.client.post("/verify/start", data={"phone": "4155550101"}).status_code == 200


def test_verify_daily_cap(env):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    env.app.state.verify_daily = {"day": today, "count": web.VERIFY_DAILY_CAP}
    r = env.client.post("/verify/start", data={"phone": "4155550100"})
    assert r.status_code == 429 and not env.verify.started
    env.app.state.verify_daily = {"day": "2000-01-01", "count": web.VERIFY_DAILY_CAP}
    assert env.client.post("/verify/start", data={"phone": "4155550100"}).status_code == 200


def test_verify_daily_cap_counts_sends(env):
    env.client.post("/verify/start", data={"phone": "4155550100"})
    assert env.app.state.verify_daily["count"] == 1


def test_crawl_rate_limited_per_ip(env):
    codes = []
    for _ in range(4):
        codes.append(env.client.post(f"/c/{env.cid}/crawl", follow_redirects=False).status_code)
        env.app.state.running.discard(env.cid)
    assert codes[:3] == [303] * 3 and codes[3] == 429


def test_add_company_rate_limited_per_ip(env):
    codes = [
        env.client.post("/companies", data={"name": "X", "number": f"888555{i:04d}"}, follow_redirects=False).status_code
        for i in range(6)
    ]
    assert codes[:5] == [303] * 5 and codes[5] == 429


@pytest.mark.parametrize("calls,minutes", [(40, 0), (0, 30)])
def test_crawl_blocked_when_company_used_up_today(env, monkeypatch, calls, minutes):
    monkeypatch.setattr(web, "calls_today", lambda conn, cid: (calls, minutes))
    r = env.client.post(f"/c/{env.cid}/crawl")
    assert r.status_code == 429 and "already mapped as much as allowed today" in r.text
    assert env.cid not in env.app.state.running
    assert db.get_node(env.conn, env.cid, "") is None


def test_crawl_allowed_just_under_daily_company_limit(env, monkeypatch):
    monkeypatch.setattr(web, "calls_today", lambda conn, cid: (39, 29.9))
    assert env.client.post(f"/c/{env.cid}/crawl", follow_redirects=False).status_code == 303


def test_duplicate_company_integrity_race_redirects(env, monkeypatch):
    def racing(conn, name, number):
        conn.execute("INSERT INTO companies (name, number) VALUES (?,?)", (name, number))
        conn.commit()
        raise sqlite3.IntegrityError("UNIQUE")
    monkeypatch.setattr(web.db, "add_company", racing)
    r = env.client.post("/companies", data={"name": "New", "number": "888 555 1111"}, follow_redirects=False)
    new = env.conn.execute("SELECT id FROM companies WHERE number='+18885551111'").fetchone()[0]
    assert r.status_code == 303 and r.headers["location"] == f"/c/{new}"


def test_handlers_hold_db_lock(env, monkeypatch):
    mapped(env)
    held = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            held.append(1)
            return self.inner.__enter__()

        def __exit__(self, *a):
            held.pop()
            return self.inner.__exit__(*a)

    monkeypatch.setattr(web.db, "LOCK", Spy(db.LOCK))
    seen = []
    real = web.tree_json
    monkeypatch.setattr(web, "tree_json", lambda c, cid: seen.append(bool(held)) or real(c, cid))
    env.client.get(f"/c/{env.cid}")
    env.client.get(f"/api/companies/{env.cid}/tree")
    assert seen == [True, True]


def test_rate_limiter_deletes_emptied_keys_and_sweeps():
    t = [0.0]
    rl = web.RateLimiter(5, 10, clock=lambda: t[0])
    for i in range(50):
        rl.allow(f"ip{i}")
    assert len(rl.hits) == 50
    t[0] = 100
    for _ in range(1000):
        rl.allow("live")
    assert set(rl.hits) == {"live"}


def test_rate_limiter_denied_key_kept():
    rl = web.RateLimiter(1, 10, clock=lambda: 0.0)
    rl.allow("a")
    assert not rl.allow("a") and "a" in rl.hits


def test_security_headers_and_static_htmx(env):
    for r in (env.client.get("/"), env.client.get("/static/htmx.min.js"), env.client.get("/nope")):
        assert r.headers["content-security-policy"] == web.CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in web.CSP and "script-src 'self'" in web.CSP
    assert env.client.get("/static/htmx.min.js").status_code == 200


def test_docs_disabled(env):
    for u in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(u).status_code == 404


def test_no_inline_script_and_local_htmx(env):
    mapped(env)
    for url in ("/", f"/c/{env.cid}"):
        html = env.client.get(url).text
        assert 'src="/static/htmx.min.js"' in html and "unpkg" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\son\w+=", html)


def test_htmx_errors_are_200_partials(env):
    mapped(env)
    r = env.client.post("/verify/start", data={"phone": "123"}, headers=HX)
    assert r.status_code == 200 and "<p>" in r.text
    r = env.client.post("/verify/check", data={"phone": "4155550100", "code": "000000"}, headers=HX)
    assert r.status_code == 200 and "Wrong or expired code" in r.text
    r = env.client.post("/verify/start", data={"phone": "4155550100"}, headers={**HX, **EVIL})
    assert r.status_code == 200 and not env.verify.started
    for _ in range(25):
        r = env.client.post(f"/c/{env.cid}/ask", data={"question": "q"}, headers=HX)
    assert r.status_code == 200 and "Too many requests" in r.text


def test_htmx_error_message_still_escaped(env):
    env.verify.start = lambda e: (_ for _ in ()).throw(RuntimeError("<b>x</b>"))
    r = env.client.post("/verify/start", data={"phone": "4155550100"}, headers=HX)
    assert "<b>" not in r.text


def test_non_htmx_keeps_real_status(env):
    assert env.client.post("/verify/start", data={"phone": "123"}).status_code == 400


def test_htmx_does_not_mask_other_routes(env):
    assert env.client.post("/c/99/crawl", headers=HX).status_code == 404


def _audio_obs(env, tmp_path, outcome):
    f = tmp_path / "audio" / f"{outcome}.wav"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"RIFF")
    env.conn.execute(
        "INSERT INTO observations (company_id, path, raw_json, audio_url, created_at) VALUES (?,?,?,?,?)",
        (env.cid, "7", '{"outcome": "%s"}' % outcome, str(f), "t"),
    )
    return env.conn.execute("SELECT MAX(id) FROM observations").fetchone()[0]


@pytest.mark.parametrize("outcome", ["human", "voicemail"])
def test_audio_of_real_people_is_404(env, tmp_path, outcome):
    oid = _audio_obs(env, tmp_path, outcome)
    assert env.client.get(f"/audio/{oid}").status_code == 404


def test_audio_of_menu_still_served(env, tmp_path):
    assert env.client.get(f"/audio/{_audio_obs(env, tmp_path, 'menu')}").status_code == 200


def test_tree_does_not_link_human_audio(env):
    mapped(env)
    human = env.conn.execute(
        "SELECT id FROM observations WHERE path='2' AND audio_url IS NOT NULL").fetchone()[0]
    page = env.client.get(f"/c/{env.cid}").text
    assert f"/audio/{human}\"" not in page and "/audio/" in page
