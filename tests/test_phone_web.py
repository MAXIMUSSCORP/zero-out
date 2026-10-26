import dataclasses
import io
import struct
import sys
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import zeroout
from tests.test_web import LOCAL, PORT, TOKEN, env, web  # noqa: F401
from zeroout.models import Observation, Option

LOCAL_ORIGIN = {"Origin": LOCAL}
WAV = {"Content-Type": "audio/wav"}
NO_MODEL = "The speech recognition model is not installed on this computer."


def tiny_wav(seconds=0.1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))
    return buf.getvalue()


def step(path, press_now=None):
    keys = path.split(".") if path else []
    return SimpleNamespace(path=path, keys=keys, press_now=press_now, label="L" + path)


class FakePhone:
    """Stands in for zeroout.phone: records what the routes hand it."""

    def __init__(self):
        self.runs_started, self.heard_calls, self.kept, self.skips_seen = 0, [], [], []
        self.obs = Observation("press 1 for billing", [Option("1", "billing"), Option("2", "<b>x</b>")], "menu", 4.0)
        self.hear_exc = self.keep_exc = None
        self.result = "mapped"
        self.gate = None  # a callable run inside hear_clip

    def start_phone_run(self, conn, company_id):
        self.runs_started += 1
        return conn.execute(
            "INSERT INTO runs (company_id, kind, started_at) VALUES (?, 'phone', 'now')",
            (company_id,)).lastrowid

    def retry_step(self, path):
        return step(path, press_now=path.rpartition(".")[2] or None)

    def next_step(self, conn, company_id, current, max_depth=4, skip=frozenset()):
        self.skips_seen.append(frozenset(skip))
        return None if "9" in skip else step("9")

    def hear_clip(self, settings, wav, *, capped, sound, **kw):
        if self.gate:
            self.gate()
        if self.hear_exc:
            raise self.hear_exc
        self.heard_calls.append((wav, capped, sound))
        if self.obs is None:
            return SimpleNamespace(obs=None, message="Nothing usable was heard.")
        return SimpleNamespace(obs=self.obs, message="heard")

    def keep_clip(self, conn, company_id, path, obs, *, run_id, same_meaning):
        if self.keep_exc:
            raise self.keep_exc
        self.kept.append((path, obs, run_id))
        conn.execute("UPDATE companies SET name = name || '!' WHERE id = ?", (company_id,))
        return SimpleNamespace(
            outcome=obs.outcome, options=obs.options, result=self.result,
            message="Kept it.", next=step("1"))


@pytest.fixture
def fake(monkeypatch):
    f = FakePhone()
    monkeypatch.setitem(sys.modules, "zeroout.phone", f)
    monkeypatch.setattr(zeroout, "phone", f, raising=False)
    return f


def local_client(env, listen=True):
    st = dataclasses.replace(env.st, owner_number="+14155550100")
    kw = {"listen": env.menu.listen} if listen else {}
    app = web.create_app(
        st, env.conn, probe=env.menu.probe, same=lambda a, b: True,
        local=web.LocalGuard(PORT, TOKEN), **kw)
    c = TestClient(app, base_url=LOCAL)
    assert c.get(f"/start?t={TOKEN}", follow_redirects=False).status_code == 303
    c.app_obj = app
    return c


def clip(c, env, path="", wav=None, capped=0, sound=1, headers=None):
    return c.post(f"/c/{env.cid}/phone/clip", params={"path": path, "capped": capped, "sound": sound},
                  content=wav if wav is not None else tiny_wav(),
                  headers={**LOCAL_ORIGIN, **WAV, **(headers or {})})


def act(c, env, name, payload, cid=None, headers=LOCAL_ORIGIN):
    return c.post(f"/c/{cid or env.cid}/phone/{name}", json=payload, headers=headers)


def name_of(env):
    return env.conn.execute("SELECT name FROM companies WHERE id = ?", (env.cid,)).fetchone()["name"]


def test_hosted_404_for_all_routes(env, fake):
    for method, tail in (("get", ""), ("post", "/clip"), ("post", "/keep"), ("post", "/discard"), ("post", "/skip")):
        r = getattr(env.client, method)(f"/c/{env.cid}/phone{tail}")
        assert r.status_code == 404, tail
    assert "/phone" not in env.client.get(f"/c/{env.cid}").text


def test_company_page_links_phone_mode_locally(env, fake):
    r = local_client(env).get(f"/c/{env.cid}")
    assert f'href="/c/{env.cid}/phone"' in r.text and "Map with my phone" in r.text


def test_page_content_and_copy(env, fake):
    r = local_client(env).get(f"/c/{env.cid}/phone")
    assert r.status_code == 200
    assert "1-800-555-0000" in r.text and "Acme" in r.text
    assert 'src="/static/phone.js"' in r.text and "<script>" not in r.text
    assert "Nebius Token Factory" in r.text and "the audio itself is never saved" in r.text
    assert "Heard on this computer by NVIDIA Parakeet; read by " in r.text
    assert "click Stop before you speak to them" in r.text
    assert "Skip this option" in r.text and "Keep" in r.text and "Discard" in r.text


def test_page_ids_keypad_and_no_inline_style(env, fake):
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "<style" not in t and 'class="back"' in t and "Acme" in t
    for i in ("next", "start", "stop", "skip", "status", "meter", "error", "message",
              "heard-text", "options", "review", "keep", "discard"):
        assert f'id="{i}"' in t, i
    for a in ("data-has-step", "data-path", "data-keys", "data-press-now", "data-label", "data-company"):
        assert a in t, a
    assert t.count('data-key="') == 12 and "PQRS" in t and "WXYZ" in t
    assert 'class="btn primary"' in t and 'id="error" class="notice strong"' in t
    assert "<h1" in t and "Map 9</h1>" in t and 'class="note"' in t


def test_page_order_and_heard_hidden_at_load(env, fake):
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert t.index('id="headline"') < t.index('id="lede"') < t.index('id="keypad"') < t.index('id="next"')
    assert t.index('id="start"') < t.index("click Stop before") < t.index("Nebius") < t.index('id="heard"')
    assert '<section id="heard" hidden>' in t and "1-800-555-0000" in t
    assert "branch" not in t.lower() and "Skip this option" in t


def test_page_lights_the_key_to_press_now(env, fake):
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert t.count("key lit") == 2  # the brand cap in the bar and the keypad key
    assert '<span class="key lit" data-key="9">' in t and '<span class="key" data-key="1">' in t


def test_page_lights_nothing_when_there_is_no_step(env, fake):
    fake.next_step = lambda *a, **k: None
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert 'class="key lit" data-key' not in t and "Everything reachable is mapped" in t


def test_page_unknown_company(env, fake):
    assert local_client(env).get("/c/999/phone").status_code == 404


def test_permissions_policy_header_local_only(env, fake):
    assert local_client(env).get(f"/c/{env.cid}").headers["Permissions-Policy"] == "microphone=(self)"
    assert "Permissions-Policy" not in env.client.get("/").headers


def test_clip_raw_body_and_flags_pass_through_and_store_nothing(env, fake):
    c = local_client(env)
    wav = tiny_wav(0.2)
    r = clip(c, env, path="1", wav=wav, capped=1, sound=0)
    assert r.status_code == 200
    j = r.json()
    assert fake.heard_calls == [(wav, True, False)]
    assert j["message"] == "Check what was heard, then keep it or discard it."
    assert j["heard"] == {
        "outcome": "menu", "prompt_text": "press 1 for billing",
        "options": [{"key": "1", "label": "Billing"}, {"key": "2", "label": "<b>x</b>"}],
        "lines": [{"text": "press 1 for billing", "key": "1"}]}
    assert j["nonce"] and fake.kept == [] and name_of(env) == "Acme"
    assert env.conn.execute("SELECT COUNT(*) n FROM observations").fetchone()["n"] == 0


def test_clip_nothing_heard_returns_retry(env, fake):
    fake.obs = None
    j = clip(local_client(env), env, path="2").json()
    assert j["heard"] is None and j["message"] == "Nothing usable was heard."
    assert j["next"] == {"path": "2", "keys": ["2"], "press_now": "2", "label": "L2"} and "nonce" not in j


@pytest.mark.parametrize("bad", ["x", "1..2", "1.", "12", "a.b"])
def test_bad_path_is_400(env, fake, bad):
    r = clip(local_client(env), env, path=bad)
    assert r.status_code == 400 and "error" in r.json() and not fake.heard_calls


def test_multipart_is_not_parsed(env, fake):
    r = local_client(env).post(
        f"/c/{env.cid}/phone/clip", params={"path": ""}, files={"audio": ("c.wav", tiny_wav(), "audio/wav")},
        headers=LOCAL_ORIGIN)
    assert r.status_code == 415 and not fake.heard_calls


def test_value_error_is_400_plain(env, fake):
    fake.hear_exc = ValueError("secret detail")
    r = clip(local_client(env), env)
    assert r.status_code == 400 and r.json() == {"error": "The recording could not be read. Try again."}


def test_runtime_error_is_503_with_phone_message(env, fake):
    fake.hear_exc = RuntimeError("model files missing")
    r = clip(local_client(env), env)
    assert r.status_code == 503 and r.json() == {"error": NO_MODEL}


def test_other_error_is_500_plain_and_releases_the_company(env, fake):
    c = local_client(env)
    fake.hear_exc = KeyError("secret detail")
    r = clip(c, env)
    assert r.status_code == 500 and r.json() == {"error": "Something went wrong hearing that clip."}
    fake.hear_exc = None
    assert clip(c, env).status_code == 200


def test_missing_model_checked_before_clip_when_no_fake_listen(env, fake, monkeypatch):
    from zeroout import hear

    def boom(d):
        raise RuntimeError("no model")

    monkeypatch.setattr(hear, "load", boom)
    r = clip(local_client(env, listen=False), env)
    assert r.status_code == 503 and r.json() == {"error": NO_MODEL} and not fake.heard_calls


def test_oversize_content_length_is_413(env, fake):
    r = clip(local_client(env), env, wav=b"\0" * (web.PHONE_MAX_BYTES + 5000))
    assert r.status_code == 413 and "error" in r.json() and not fake.heard_calls


def test_oversize_stream_without_content_length_is_413(env, fake):
    def body():
        for _ in range(web.PHONE_MAX_BYTES // 65536 + 2):
            yield b"\0" * 65536

    r = local_client(env).post(f"/c/{env.cid}/phone/clip", content=body(), headers={**LOCAL_ORIGIN, **WAV})
    assert r.status_code == 413 and not fake.heard_calls


def test_rate_limit(env, fake, monkeypatch):
    monkeypatch.setattr(web, "PHONE_CLIPS_PER_HOUR", 2)
    c = local_client(env)
    assert [clip(c, env).status_code for _ in range(3)] == [200, 200, 429]


@pytest.mark.parametrize("name,payload", [("keep", {"nonce": "x"}), ("discard", {"nonce": "x"}), ("skip", {"path": "1"})])
def test_foreign_origin_is_403(env, fake, name, payload):
    c = local_client(env)
    assert act(c, env, name, payload, headers={"Origin": "http://evil.example"}).status_code == 403
    assert clip(c, env, headers={"Origin": "http://evil.example"}).status_code == 403
    assert not fake.heard_calls


def test_keep_merges_only_after_keep(env, fake):
    c = local_client(env)
    nonce = clip(c, env, path="1").json()["nonce"]
    assert name_of(env) == "Acme"
    r = act(c, env, "keep", {"nonce": nonce})
    assert r.status_code == 200
    j = r.json()
    assert name_of(env) == "Acme!" and fake.kept[0][0] == "1" and fake.kept[0][1] is fake.obs
    assert j["result"] == "mapped" and j["message"] == "Kept it." and j["outcome"] == "menu"
    assert j["options"][0] == {"key": "1", "label": "Billing"}
    assert j["next"]["path"] == "9"  # computed by the route with the skip set, not keep_clip's own
    assert act(c, env, "keep", {"nonce": nonce}).status_code == 404  # single use


def test_keep_wrong_nonce_wrong_cid_expired_is_404(env, fake, monkeypatch):
    c = local_client(env)
    other = env.conn.execute("INSERT INTO companies (name, number) VALUES ('B', '+18005550001')").lastrowid
    msg = {"error": "That clip is no longer waiting. Record it again."}
    r = act(c, env, "keep", {"nonce": "nope"})
    assert r.status_code == 404 and r.json() == msg
    n = clip(c, env).json()["nonce"]
    assert act(c, env, "keep", {"nonce": n}, cid=other).status_code == 404
    n = clip(c, env).json()["nonce"]
    real = web.time.monotonic()
    monkeypatch.setattr(web.time, "monotonic", lambda: real + 601)
    assert act(c, env, "keep", {"nonce": n}).status_code == 404
    assert fake.kept == []


def test_keep_bad_body_is_400(env, fake):
    c = local_client(env)
    r = c.post(f"/c/{env.cid}/phone/keep", content=b"nope",
               headers={**LOCAL_ORIGIN, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert act(c, env, "keep", {"nonce": 5}).status_code == 400


def test_keep_errors_are_plain(env, fake):
    c = local_client(env)
    fake.keep_exc = KeyError("secret")
    r = act(c, env, "keep", {"nonce": clip(c, env).json()["nonce"]})
    assert r.status_code == 500 and r.json() == {"error": "Something went wrong keeping that clip."}


def test_discard(env, fake):
    c = local_client(env)
    n = clip(c, env, path="1").json()["nonce"]
    r = act(c, env, "discard", {"nonce": n})
    assert r.status_code == 200
    assert r.json()["message"] == "Discarded. Nothing was kept."
    assert r.json()["next"]["path"] == "1" and name_of(env) == "Acme"
    assert act(c, env, "keep", {"nonce": n}).status_code == 404


def test_skip_adds_to_skip_set(env, fake):
    c = local_client(env)
    r = act(c, env, "skip", {"path": "9"})
    assert r.json() == {"message": "Skipped that option.", "next": None, "skipped": 1}
    assert "9" in fake.skips_seen[-1]
    assert act(c, env, "skip", {"path": "x"}).status_code == 400


def test_two_unusable_clips_auto_skip(env, fake):
    c = local_client(env)
    fake.obs = None
    first = clip(c, env, path="3").json()
    assert first["next"]["path"] == "3" and "moved on" not in first["message"]
    second = clip(c, env, path="3").json()
    assert second["message"].endswith("Zero-Out moved on from that option after two tries.")
    assert "3" in fake.skips_seen[-1] and second["next"]["path"] == "9"


def test_kept_retry_results_auto_skip(env, fake):
    c = local_client(env)
    fake.result = "retry"
    fake.obs = Observation("not a valid option", [], "invalid_input", 1.0)
    out = []
    for _ in range(2):
        n = clip(c, env, path="3").json()["nonce"]
        out.append(act(c, env, "keep", {"nonce": n}).json())
    assert out[0]["next"]["path"] == "3" and "moved on" not in out[0]["message"]
    assert out[1]["message"].endswith("Zero-Out moved on from that option after two tries.")
    assert "3" in fake.skips_seen[-1]


def test_run_created_once_and_not_in_cookie(env, fake):
    c = local_client(env)
    for _ in range(2):
        act(c, env, "keep", {"nonce": clip(c, env).json()["nonce"]})
    assert fake.runs_started == 1 and fake.kept[0][2] == fake.kept[1][2]
    import base64, json
    payload = c.cookies.get("session").split(".")[0]
    session = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert "phone_runs" not in session
    env.conn.execute("DELETE FROM runs")
    act(c, env, "keep", {"nonce": clip(c, env).json()["nonce"]})
    assert fake.runs_started == 2


def test_one_clip_at_a_time_per_company(env, fake):
    c = local_client(env)
    seen = {}

    def gate():
        fake.gate = None
        seen["second"] = clip(c, env)

    fake.gate = gate
    first = clip(c, env)
    assert first.status_code == 200
    assert seen["second"].status_code == 409
    assert seen["second"].json() == {"error": "Still hearing the last clip."}
    assert clip(c, env).status_code == 200


def test_refused_while_automatic_crawl_runs(env, fake):
    c = local_client(env)
    c.app_obj.state.running.add(env.cid)
    r = clip(c, env)
    assert r.status_code == 409 and r.json() == {"error": "Already mapping this company with automatic calls."}
    assert not fake.heard_calls


def test_wav_encoder_and_helpers_in_node():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    assert node, "node is required to test the browser helpers"
    script = Path(__file__).parent / "js" / "test_wav.mjs"
    r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "ok"


def test_real_phone_module_end_to_end(env, monkeypatch):
    from zeroout import phone

    obs = Observation("press 1 for billing or 2 for sales", [Option("1", "billing"), Option("2", "sales")], "menu", 4.0)
    real = phone.hear_clip
    monkeypatch.setattr(
        phone, "hear_clip", lambda st, wav, *, capped, sound: real(st, wav, capped=capped, sound=sound, hear=lambda s, w: (True, obs)))
    c = local_client(env)
    page = c.get(f"/c/{env.cid}/phone")
    assert page.status_code == 200 and "Click Start listening when it answers." in page.text and "Map the main menu" in page.text and "on speaker" in page.text
    heard = clip(c, env).json()
    assert heard["heard"]["prompt_text"] == obs.prompt_text
    assert env.conn.execute("SELECT COUNT(*) n FROM observations").fetchone()["n"] == 0
    kept = act(c, env, "keep", {"nonce": heard["nonce"]})
    assert kept.status_code == 200, kept.text
    j = kept.json()
    assert j["outcome"] == "menu" and [o["key"] for o in j["options"]] == ["1", "2"]
    assert j["next"]["path"] == "1" and j["next"]["press_now"] == "1"
    assert env.conn.execute("SELECT COUNT(*) n FROM observations").fetchone()["n"] == 1
    assert env.conn.execute("SELECT kind FROM runs").fetchone()["kind"] == "phone"
    skipped = act(c, env, "skip", {"path": "1"}).json()
    assert skipped["next"]["path"] == "2"


def test_root_is_never_auto_skipped(env, fake):
    c = local_client(env)
    fake.obs = None
    for _ in range(3):
        j = clip(c, env, path="").json()
        assert j["next"]["path"] == "" and "moved on" not in j["message"]
    assert all("" not in s for s in fake.skips_seen)


def test_kept_retry_at_root_is_never_auto_skipped(env, fake):
    c = local_client(env)
    fake.result = "retry"
    fake.obs = Observation("nope", [], "invalid_input", 1.0)
    for _ in range(3):
        j = act(c, env, "keep", {"nonce": clip(c, env, path="").json()["nonce"]}).json()
        assert j["next"]["path"] == "" and "moved on" not in j["message"]


def test_responses_say_how_many_branches_were_skipped(env, fake):
    c = local_client(env)
    assert act(c, env, "skip", {"path": "9"}).json()["skipped"] == 1
    fake.obs = None
    assert clip(c, env, path="3").json()["skipped"] == 1
    j = clip(c, env, path="3").json()
    assert j["skipped"] == 2 and j["next"] is None
    n = clip(c, env, path="4")  # still no obs, one failure
    assert n.json()["skipped"] == 2


def test_keep_refused_while_crawl_runs(env, fake):
    c = local_client(env)
    n = clip(c, env).json()["nonce"]
    c.app_obj.state.running.add(env.cid)
    r = act(c, env, "keep", {"nonce": n})
    assert r.status_code == 409 and r.json() == {"error": "Already mapping this company with automatic calls."}
    assert fake.kept == []
    c.app_obj.state.running.discard(env.cid)
    assert act(c, env, "keep", {"nonce": n}).status_code == 200  # the clip was not consumed


def test_good_keep_resets_the_failure_count(env, fake):
    c = local_client(env)
    fake.obs = None
    assert "moved on" not in clip(c, env, path="3").json()["message"]
    fake.obs = Observation("press 1", [Option("1", "a")], "menu", 1.0)
    act(c, env, "keep", {"nonce": clip(c, env, path="3").json()["nonce"]})
    fake.obs = None
    j = clip(c, env, path="3").json()
    assert "moved on" not in j["message"] and j["next"]["path"] == "3"


def test_expired_pending_clips_are_swept(env, fake, monkeypatch):
    c = local_client(env)
    clip(c, env), clip(c, env)
    pending = c.app_obj.state.phone["pending"]
    assert len(pending) == 2
    real = web.time.monotonic()
    monkeypatch.setattr(web.time, "monotonic", lambda: real + 601)
    clip(c, env)
    assert len(pending) == 1


def test_disputed_keep_skips_the_path_at_once(env, fake):
    c = local_client(env)
    fake.result = "disputed"
    j = act(c, env, "keep", {"nonce": clip(c, env, path="3").json()["nonce"]}).json()
    assert "3" in fake.skips_seen[-1] and j["next"]["path"] != "3"
    assert j["message"].endswith("Zero-Out moved on; an automatic call can check that option later.")
    assert j["skipped"] == 1


def test_disputed_keep_at_root_is_not_skipped(env, fake):
    c = local_client(env)
    fake.result = "disputed"
    j = act(c, env, "keep", {"nonce": clip(c, env, path="").json()["nonce"]}).json()
    assert "" not in fake.skips_seen[-1] and "moved on" not in j["message"]


def test_reloaded_page_says_branches_were_skipped(env, fake):
    c = local_client(env)
    assert "heard every option it can reach" not in c.get(f"/c/{env.cid}/phone").text
    fake.next_step = lambda *a, **k: None
    assert "Zero-Out has heard every option it can reach" in c.get(f"/c/{env.cid}/phone").text
    act(c, env, "skip", {"path": "9"})
    t = c.get(f"/c/{env.cid}/phone").text
    assert "Every option Zero-Out could reach is mapped or was skipped. Hang up." in t
    assert "heard every option it can reach" not in t


def test_page_names_the_nano_model_in_one_privacy_hint(env, fake):
    env.st = dataclasses.replace(env.st, model_nano="some/other-model")
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "read by some/other-model on Nebius Token Factory" in t
    assert t.count("Nebius Token Factory") == 1


def test_page_says_the_language_model_when_none_is_set(env, fake):
    env.st = dataclasses.replace(env.st, model_nano="")
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "read by the language model on Nebius Token Factory" in t and "read by  on" not in t


def test_page_shows_the_friendly_name_of_a_known_model(env, fake):
    env.st = dataclasses.replace(env.st, model_nano="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "read by NVIDIA Nemotron 3 Nano on Nebius Token Factory" in t


def test_heard_transcript_is_a_quote_and_options_are_rows(env, fake):
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert '<div id="heard-quote" class="quote" hidden>' in t and "What the menu says" in t
    assert '<ul id="heard-text" hidden>' in t
    assert 'id="options-label" class="label" hidden>Options found' in t
    assert '<ul id="options" class="list">' in t


def test_opening_with_a_next_step_puts_keys_in_the_headline_and_the_call_once_in_next(env, fake):
    fake.next_step = lambda *a, **k: SimpleNamespace(
        path="2.1", keys=["2", "1"], press_now=None, label="costco city visa")
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "Map 2, then 1</h1>" in t
    assert '<p id="lede" class="lede">Costco city visa</p>' in t
    assert ("Call 1-800-555-0000, press 2, then 1, waiting for each menu to finish, "
            "then click Start listening right after your last key.") in t and 'data-number="1-800-555-0000"' in t
    assert t.count("1-800-555-0000") == 2  # #next and its data-number; the lede and headline do not repeat it
    assert "again" not in t.lower().split('id="headline"')[1].split('id="keypad"')[0]
    assert 'id="restart" type="button" class="btn primary" hidden' in t


def test_opening_lede_without_a_label_is_the_call_sentence_and_a_long_label_is_cut(env, fake):
    c = local_client(env)
    fake.next_step = lambda *a, **k: SimpleNamespace(path="2.1", keys=["2", "1"], press_now=None, label="  ")
    t = c.get(f"/c/{env.cid}/phone").text
    assert "Map 2, then 1</h1>" in t and "on your phone, on speaker, next to this computer." in t
    fake.next_step = lambda *a, **k: SimpleNamespace(path="2", keys=["2"], press_now=None, label="<b>" + "x" * 120)
    lede = c.get(f"/c/{env.cid}/phone").text.split('id="lede" class="lede">')[1].split("</p>")[0]
    assert lede.startswith("&lt;b&gt;") and lede.count("…") == 1 and "<b>" not in lede


def test_opening_at_the_root_says_map_the_main_menu(env, fake):
    fake.next_step = lambda *a, **k: SimpleNamespace(path="", keys=[], press_now=None, label="")
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "Map the main menu</h1>" in t and "on your phone, on speaker, next to this computer." in t
    assert "Click Start listening when it answers." in t and "Call 1-800-555-0000 and" not in t


def test_opening_with_nothing_left_offers_to_map_the_main_menu_again(env, fake):
    fake.next_step = lambda *a, **k: None
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "Everything reachable is mapped</h1>" in t
    assert "Call only if you want to check whether the menu has changed." in t and "on speaker" not in t
    assert "Zero-Out has heard every option it can reach. Options that need a person, account details or speech, that replay a menu, or that sit more than four menus deep are left out." in t
    assert 'id="restart" type="button" class="btn primary">Map the main menu again' in t
    assert 'id="start" type="button" class="btn primary" hidden' in t
    assert 'id="skip" type="button" class="btn secondary" hidden' in t
    assert 'class="key lit" data-key' not in t and "Hang up" not in t


def voice_root(env, status="speech_menu"):
    from zeroout import tree
    tree.ensure_root(env.conn, env.cid)
    env.conn.execute("UPDATE nodes SET status = ? WHERE company_id = ? AND path = ''", (status, env.cid))


def test_a_voice_assistant_root_says_so_instead_of_everything_is_mapped(env, fake):
    fake.next_step = lambda *a, **k: None
    voice_root(env)
    t = local_client(env).get(f"/c/{env.cid}/phone").text
    assert "This line uses a voice assistant</h1>" in t
    assert "It asks you to say what you need, so there are no keys to map." in t
    assert "Call only to check whether it has changed to a keypad menu." in t
    assert "Everything reachable is mapped" not in t and "heard every option" not in t
    assert 'id="restart" type="button" class="btn primary">Map the main menu again' in t
    assert 'class="key lit" data-key' not in t


def test_a_voice_root_wins_over_skipped_branches_and_a_mapped_root_does_not_say_it(env, fake):
    c = local_client(env)
    fake.next_step = lambda *a, **k: None
    voice_root(env)
    act(c, env, "skip", {"path": "9"})
    assert "This line uses a voice assistant" in c.get(f"/c/{env.cid}/phone").text
    voice_root(env, "mapped")
    assert "voice assistant" not in c.get(f"/c/{env.cid}/phone").text


def test_a_voice_root_with_a_step_left_still_opens_on_the_step(env, fake):
    voice_root(env)
    assert "voice assistant" not in local_client(env).get(f"/c/{env.cid}/phone").text


def test_heard_and_kept_options_show_derived_labels(env, fake):
    fake.obs = Observation("For billing questions, press 1. For Costco Travel, press 5.",
                           [Option("1", "press 1"), Option("5", "")], "menu", 4.0)
    c = local_client(env)
    j = clip(c, env, path="").json()
    want = [{"key": "1", "label": "Billing questions"}, {"key": "5", "label": "Costco Travel"}]
    assert j["heard"]["options"] == want
    assert act(c, env, "keep", {"nonce": j["nonce"]}).json()["options"] == want
