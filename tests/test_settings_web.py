import dataclasses
import os
import re
import stat
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from tests.fakes import FakeMenu
from tests.test_setup import IDS, KEY, NANO, SID, SUPER, Clock, FakeTwilio, Http, models_response, refused
from tests.test_web import LOCAL, PORT, TOKEN, YES, fake_tree
from zeroout import config, db, hear, settings_page, web

OLD_KEY = "oldkey-0123456789abcd"
TOKEN_SECRET = "tw" + "9f" * 15
NUMBER = "+14155550100"
ORIGIN = {"Origin": LOCAL}
HX = {"HX-Request": "true", "Origin": LOCAL}
SESSION = "x" * 48


class Page:
    pass


def write_env(path, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"TF_API_KEY={OLD_KEY}\nTF_BASE_URL=https://api.tokenfactory.nebius.com/v1/\n"
        f"MODEL_NANO={NANO}\nMODEL_SUPER={SUPER}\nSESSION_SECRET={SESSION}\nCOOKIE_SECURE=0\n"
        f"DAILY_CAP_USD=5\nKEEP_ME=kept\n{extra}")


@pytest.fixture
def pg(tmp_path, monkeypatch):
    p = Page()
    p.file = tmp_path / "cfg" / "zeroout.env"
    write_env(p.file)
    monkeypatch.setenv("ZEROOUT_ENV_FILE", str(p.file))
    for k in ("TF_API_KEY", "TWILIO_TOKEN", "TWILIO_SID", "TF_BASE_URL", "OWNER_NUMBER", "COMMUNITY_REPO"):
        monkeypatch.delenv(k, raising=False)
    p.mp = monkeypatch
    p.st = dataclasses.replace(config.load_settings(), cookie_secure=False, audio_dir=str(tmp_path / "audio"))
    p.conn = db.connect(":memory:")
    p.cid = db.add_company(p.conn, "Acme", "+18005550000")
    p.http = Http(models_response(IDS))
    p.twilio = FakeTwilio()
    p.twilio_calls = []
    p.clock = Clock()
    p.checked_models = []
    p.models = tmp_path / "models"

    def make_twilio(sid, token):
        p.twilio_calls.append((sid, token))
        return p.twilio

    p.services = settings_page.Services(
        http=lambda *a, **k: p.http(*a, **k), twilio=make_twilio, clock=p.clock.now,
        verify_model=p.checked_models.append, github_login=lambda: "octo-cat")
    monkeypatch.setenv("ZEROOUT_HOME", str(tmp_path / "zhome"))
    p.menu = FakeMenu(fake_tree())
    p.connect_seen = []

    def connect(conn, st, cid, path, phone, *, on_done=None, **kw):
        p.connect_seen.append((st.owner_number, st.twilio_from, phone))
        on_done()
        return 1

    p.connect = connect

    def build(**kw):
        app = web.create_app(
            p.st, p.conn, probe=kw.pop("probe", p.menu.probe), listen=kw.pop("listen", p.menu.listen), same=YES,
            connect=p.connect, local=web.LocalGuard(PORT, TOKEN), services=p.services, **kw)
        c = TestClient(app, base_url=LOCAL)
        assert c.get(f"/start?t={TOKEN}", follow_redirects=False).status_code == 303
        return c

    p.build = build
    p.c = build()
    p.vals = lambda: dotenv_values(p.file, interpolate=False)
    return p


def post(p, path, **data):
    return p.c.post(path, data=data, headers=HX)


def no_secrets(r, *secrets):
    for s in secrets:
        assert s not in r.text, "a secret reached the browser"
    assert "!" not in re.sub(r"<!--.*?-->|<!doctype html>", "", r.text, flags=re.S | re.I).replace("!important", "")


# ---------- local only ----------

ROUTES = [
    ("get", "/settings"), ("post", "/settings/model"), ("post", "/settings/model/check"),
    ("post", "/settings/phone"), ("post", "/settings/phone/verify"), ("post", "/settings/phone/verify/status"),
    ("post", "/settings/speech"), ("post", "/settings/spending"), ("post", "/settings/community"),
    ("get", "/settings/community/account"), ("post", "/settings/phone/stop"), ("get", "/settings/phone/number"),
]


def test_every_settings_route_is_404_when_hosted(pg):
    st = dataclasses.replace(pg.st, owner_number="")
    c = TestClient(web.create_app(st, pg.conn, probe=pg.menu.probe, listen=pg.menu.listen, same=YES,
                                  services=pg.services))
    for method, path in ROUTES:
        r = getattr(c, method)(path, **({"data": {}, "headers": HX} if method == "post" else {}))
        assert r.status_code == 404, path
    assert c.get("/settings", headers={"HX-Request": "true"}).status_code == 404


def test_launch_session_and_origin_still_guard_settings(pg):
    app = web.create_app(pg.st, pg.conn, probe=pg.menu.probe, listen=pg.menu.listen, same=YES,
                         local=web.LocalGuard(PORT, TOKEN), services=pg.services)
    bare = TestClient(app, base_url=LOCAL)
    assert bare.get("/settings").status_code == 403
    assert bare.post("/settings/spending", data={"cap": "9"}, headers=ORIGIN).status_code == 403
    r = pg.c.post("/settings/spending", data={"cap": "9"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert pg.vals()["DAILY_CAP_USD"] == "5"


def test_http_error_shows_settings_errors_as_partials(pg):
    for _ in range(settings_page.CHECKS_PER_HOUR):
        post(pg, "/settings/spending", cap="5")
    r = post(pg, "/settings/spending", cap="5")
    assert r.status_code == 200 and "Too many" in r.text


# ---------- the page ----------

def test_page_has_the_sections_the_file_and_no_secret(pg):
    r = pg.c.get("/settings")
    assert r.status_code == 200
    for title in ("Language model", "Phone calls", "Speech", "Spending", "Community", "Appearance"):
        assert title in r.text
    assert str(pg.file) in r.text
    assert "Saved, ends in abcd" in r.text
    no_secrets(r, OLD_KEY)
    assert 'style="' not in r.text and "<script>" not in r.text.replace('<script src', "")
    assert re.search(r'class="seg"', r.text)
    for t in ("light", "dark", "system"):
        assert f'data-theme-set="{t}"' in r.text


def test_page_names_home_file_when_no_env_file_is_named(pg, tmp_path):
    pg.mp.delenv("ZEROOUT_ENV_FILE")
    from zeroout import home
    r = pg.c.get("/settings")
    assert str(home.config_file()) in r.text


# ---------- language model ----------

def test_model_save_checks_writes_reloads_and_keeps_other_keys(pg):
    r = post(pg, "/settings/model", api_key=KEY, base_url="https://tf.example/v1/", nano=NANO, super=SUPER)
    assert r.status_code == 200
    no_secrets(r, KEY, OLD_KEY)
    assert "Saved" in r.text and "3 models listed" in r.text
    req, _ = pg.http.requests[0]
    assert req.full_url == "https://tf.example/v1/models" and req.get_header("Authorization") == f"Bearer {KEY}"
    v = pg.vals()
    assert v["TF_API_KEY"] == KEY and v["TF_BASE_URL"] == "https://tf.example/v1/"
    assert v["KEEP_ME"] == "kept" and v["SESSION_SECRET"] == SESSION and v["DAILY_CAP_USD"] == "5"
    assert stat.S_IMODE(os.stat(pg.file).st_mode) == 0o600
    assert pg.st.tf_api_key == KEY and pg.st.tf_base_url == "https://tf.example/v1/"
    assert pg.st.session_secret == SESSION and pg.st.cookie_secure is False
    assert KEY not in pg.c.get("/settings").text


def test_model_empty_key_keeps_the_saved_one(pg):
    r = post(pg, "/settings/model", api_key="", base_url="", nano=SUPER, super=NANO)
    assert "Saved" in r.text
    assert pg.http.requests[0][0].get_header("Authorization") == f"Bearer {OLD_KEY}"
    v = pg.vals()
    assert v["TF_API_KEY"] == OLD_KEY and v["MODEL_NANO"] == SUPER and v["MODEL_SUPER"] == NANO
    assert v["TF_BASE_URL"] == "https://api.tokenfactory.nebius.com/v1/"
    assert pg.st.model_nano == SUPER


def test_model_check_lists_models_in_selects_and_saves_nothing(pg):
    before = pg.file.read_text()
    r = post(pg, "/settings/model/check", api_key=KEY, base_url="", nano=NANO, super=SUPER)
    assert "3 models listed" in r.text and "<select" in r.text
    assert "nvidia/other" not in r.text  # only Nemotron models are offered
    assert pg.file.read_text() == before
    no_secrets(r, KEY)


def test_model_choice_must_come_from_the_live_list(pg):
    r = post(pg, "/settings/model", api_key=KEY, base_url="", nano="nvidia/nemotron-ghost", super=SUPER)
    assert "Nano model nvidia/nemotron-ghost is not in your Token Factory list" in r.text
    assert 'class="field bad"' in r.text
    assert pg.vals()["TF_API_KEY"] == OLD_KEY and pg.st.tf_api_key == OLD_KEY


def test_model_refused_key_is_explained_without_the_key(pg):
    pg.http = Http(refused(401))
    r = post(pg, "/settings/model", api_key=KEY, base_url="", nano=NANO, super=SUPER)
    assert "Token Factory refused the key (HTTP 401)" in r.text and "Nothing was saved" in r.text
    no_secrets(r, KEY, OLD_KEY)
    assert pg.vals()["TF_API_KEY"] == OLD_KEY


def test_model_unreachable_is_explained(pg):
    pg.http = Http(refused(500))
    r = post(pg, "/settings/model", api_key=KEY, base_url="", nano=NANO, super=SUPER)
    assert "answered HTTP 500" in r.text
    assert pg.vals()["TF_API_KEY"] == OLD_KEY


@pytest.mark.parametrize("base", ["http://tf.example/v1/", "ftp://x/", "tf.example", "javascript:alert(1)"])
def test_model_base_url_must_be_https(pg, base):
    r = post(pg, "/settings/model", api_key=KEY, base_url=base, nano=NANO, super=SUPER)
    assert "The base URL must start with https://" in r.text and f"Got {base}" in r.text
    assert pg.http.requests == [] and pg.vals()["TF_API_KEY"] == OLD_KEY


def test_model_base_url_http_to_localhost_is_allowed(pg):
    r = post(pg, "/settings/model", api_key=KEY, base_url="http://localhost:9000/v1", nano=NANO, super=SUPER)
    assert "Saved" in r.text and pg.vals()["TF_BASE_URL"] == "http://localhost:9000/v1"


@pytest.mark.parametrize("field", ["api_key", "base_url", "nano"])
def test_line_breaks_are_refused(pg, field):
    data = dict(api_key=KEY, base_url="https://tf.example/v1/", nano=NANO, super=SUPER)
    data[field] += "\nEVIL=1"
    r = post(pg, "/settings/model", **data)
    assert "cannot contain a line break" in r.text
    assert "EVIL" not in pg.file.read_text() and pg.http.requests == []
    no_secrets(r, KEY)


def test_model_key_set_in_environment_is_read_only_and_refused(pg):
    pg.mp.setenv("TF_API_KEY", "envkey-ABCDEFGH5678")
    pg.st.tf_api_key = "envkey-ABCDEFGH5678"
    r = pg.c.get("/settings")
    assert "Set in your environment as TF_API_KEY; change it there." in r.text
    no_secrets(r, "envkey-ABCDEFGH5678")
    before = pg.file.read_text()
    r = post(pg, "/settings/model", api_key=KEY, base_url="", nano=NANO, super=SUPER)
    assert "Set in your environment as TF_API_KEY; change it there." in r.text
    assert pg.file.read_text() == before and pg.st.tf_api_key == "envkey-ABCDEFGH5678"
    assert pg.http.requests == []
    no_secrets(r, KEY, "envkey-ABCDEFGH5678")


def test_blank_environment_value_does_not_lock(pg):
    pg.mp.setenv("TF_API_KEY", "   ")
    assert "Set in your environment as TF_API_KEY" not in pg.c.get("/settings").text


# ---------- phone calls ----------

def test_phone_save_checks_account_writes_and_hides_token(pg):
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number="(415) 555-0100")
    assert r.status_code == 200 and "Saved" in r.text
    no_secrets(r, TOKEN_SECRET)
    assert pg.twilio_calls == [(SID, TOKEN_SECRET)]
    v = pg.vals()
    assert (v["TWILIO_SID"], v["TWILIO_TOKEN"], v["OWNER_NUMBER"], v["TWILIO_FROM"]) == (SID, TOKEN_SECRET, NUMBER, NUMBER)
    assert v["KEEP_ME"] == "kept" and v["TF_API_KEY"] == OLD_KEY
    assert (pg.st.twilio_sid, pg.st.twilio_token, pg.st.owner_number) == (SID, TOKEN_SECRET, NUMBER)
    page = pg.c.get("/settings")
    no_secrets(page, TOKEN_SECRET)
    assert "Saved, ends in " + TOKEN_SECRET[-4:] in page.text


def test_phone_empty_token_keeps_saved_one(pg):
    post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    post(pg, "/settings/phone", sid=SID, token="", number="415 555 0199")
    assert pg.twilio_calls[-1] == (SID, TOKEN_SECRET)
    assert pg.vals()["TWILIO_TOKEN"] == TOKEN_SECRET and pg.vals()["OWNER_NUMBER"] == "+14155550199"


def test_phone_save_says_when_the_number_is_not_verified(pg):
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    assert f"{NUMBER} is not verified with Twilio yet" in r.text
    pg.twilio = FakeTwilio(caller_ids=([SimpleNamespace()],))
    assert "not verified" not in post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER).text


def test_phone_trial_account_is_called_out(pg):
    pg.twilio = FakeTwilio(acct_type="Trial")
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    assert "This Twilio account is a trial" in r.text


@pytest.mark.parametrize("sid", ["", "AC12", "XX" + "a1" * 16, SID + "0"])
def test_phone_sid_shape(pg, sid):
    r = post(pg, "/settings/phone", sid=sid, token=TOKEN_SECRET, number=NUMBER)
    assert "An Account SID is AC followed by 32 hex characters" in r.text and 'class="field bad"' in r.text
    assert pg.twilio_calls == [] and not pg.vals().get("TWILIO_SID")
    no_secrets(r, TOKEN_SECRET)


def test_phone_number_goes_through_normalize_number(pg):
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number="12345")
    assert "Enter a US number like 1-800-555-1234" in r.text and "Got 12345" in r.text
    assert pg.twilio_calls == []


def test_phone_token_required_the_first_time(pg):
    r = post(pg, "/settings/phone", sid=SID, token="", number=NUMBER)
    assert "An auth token is required" in r.text and pg.twilio_calls == []


def test_phone_twilio_refusal_is_explained_and_nothing_saved(pg):
    from twilio.base.exceptions import TwilioException
    pg.twilio = FakeTwilio(fetch_error=type("TE", (TwilioException,), {"status": 401})(f"nope {TOKEN_SECRET}"))
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    assert "Twilio refused the Account SID or auth token (HTTP 401)" in r.text and "Nothing was saved" in r.text
    no_secrets(r, TOKEN_SECRET)
    assert not pg.vals().get("TWILIO_SID")


def test_phone_error_text_never_carries_the_token(pg):
    from twilio.base.exceptions import TwilioException
    pg.twilio = FakeTwilio(fetch_error=TwilioException(f"boom {TOKEN_SECRET}"))
    r = post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    assert "Could not check with Twilio" in r.text
    no_secrets(r, TOKEN_SECRET)


def test_phone_token_in_environment_is_read_only(pg):
    pg.mp.setenv("TWILIO_TOKEN", TOKEN_SECRET)
    pg.st.twilio_token = TOKEN_SECRET
    page = pg.c.get("/settings")
    assert "Set in your environment as TWILIO_TOKEN; change it there." in page.text
    no_secrets(page, TOKEN_SECRET)
    r = post(pg, "/settings/phone", sid=SID, token="another-token-1234", number=NUMBER)
    assert "Set in your environment as TWILIO_TOKEN; change it there." in r.text
    assert not pg.vals().get("TWILIO_TOKEN")
    no_secrets(r, TOKEN_SECRET, "another-token-1234")


# ---------- verify my number ----------

def saved_phone(pg):
    post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)


def test_verify_without_saved_number_says_what_to_do(pg):
    r = post(pg, "/settings/phone/verify")
    assert "Save your Account SID, auth token and phone number first" in r.text
    assert pg.twilio.created == []


def test_verify_start_shows_the_code_and_polls_every_three_seconds(pg):
    saved_phone(pg)
    r = post(pg, "/settings/phone/verify")
    assert pg.twilio.created == [{"phone_number": NUMBER, "friendly_name": "Zero-Out"}]
    assert f"Twilio is calling {web.tel(NUMBER)}. Answer and type this code on the keypad: 123456" in r.text
    assert 'hx-trigger="every 3s"' in r.text and 'hx-post="/settings/phone/verify/status"' in r.text
    no_secrets(r, TOKEN_SECRET)


def test_verify_status_keeps_polling_until_twilio_lists_the_number(pg):
    saved_phone(pg)
    pg.twilio = FakeTwilio(caller_ids=([],), incoming=([],))
    post(pg, "/settings/phone/verify")
    pg.clock.t = 30
    r = pg.c.post("/settings/phone/verify/status", headers=HX)
    assert 'hx-trigger="every 3s"' in r.text and "123456" in r.text
    pg.twilio.outgoing_caller_ids.results = [[SimpleNamespace()]]
    r = pg.c.post("/settings/phone/verify/status", headers=HX)
    assert "every 3s" not in r.text and f"{NUMBER} is verified" in r.text
    no_secrets(r, TOKEN_SECRET)
    # done: a new request may start
    assert "already verified" in post(pg, "/settings/phone/verify").text


def test_verify_stops_after_120_seconds_and_says_so(pg):
    saved_phone(pg)
    post(pg, "/settings/phone/verify")
    pg.clock.t = 121
    r = pg.c.post("/settings/phone/verify/status", headers=HX)
    assert "every 3s" not in r.text and "did not hear the code within 120 seconds" in r.text
    assert f"{NUMBER} is not verified" in r.text
    again = post(pg, "/settings/phone/verify")
    assert "every 3s" in again.text


def test_only_one_verification_at_a_time(pg):
    saved_phone(pg)
    post(pg, "/settings/phone/verify")
    pg.clock.t = 10
    r = post(pg, "/settings/phone/verify")
    assert "Already verifying" in r.text and len(pg.twilio.created) == 1
    assert 'hx-trigger="every 3s"' in r.text


def test_verify_twilio_error_is_a_sentence(pg):
    from twilio.base.exceptions import TwilioException
    saved_phone(pg)
    pg.twilio.validation_requests.create = lambda **kw: (_ for _ in ()).throw(TwilioException("no"))
    r = post(pg, "/settings/phone/verify")
    assert "Could not check with Twilio" in r.text and "every 3s" not in r.text


def test_verify_status_without_a_verification_does_not_poll(pg):
    r = pg.c.post("/settings/phone/verify/status", headers=HX)
    assert r.status_code == 200 and "every 3s" not in r.text


# ---------- speech ----------

def install(models, name, with_files=True):
    d = models / name
    d.mkdir(parents=True)
    if with_files and name in hear.MODELS:
        for f in hear.MODELS[name]:
            (d / f).write_bytes(b"x")
    return d


def test_speech_lists_installed_and_not_installed(pg):
    v3, en = list(hear.MODELS)
    from zeroout import home
    install(home.models_dir(), v3)
    install(home.models_dir(), "not-a-model")  # not one Zero-Out knows
    pg.st.asr_model_dir = ""  # nothing in use, so no folder hint is shown
    r = pg.c.get("/settings")
    assert 'type="radio"' in r.text and r.text.count('type="radio"') == 2
    assert "Parakeet v3, recommended" in r.text and "Parakeet 110M, English only" in r.text
    assert "sherpa-onnx" not in re.sub(r'value="[^"]*"|id="[^"]*"|for="[^"]*"', "", r.text)
    assert "Not installed. Download it with zeroout setup." in r.text
    assert "will be missed Not installed" not in r.text and "will be missed. Not installed" in r.text
    assert "not-a-model" not in r.text


def test_speech_incomplete_folder_is_not_installed(pg):
    v3, _ = list(hear.MODELS)
    from zeroout import home
    install(home.models_dir(), v3, with_files=False)
    r = pg.c.get("/settings")
    assert "Download it with zeroout setup." in r.text
    assert post(pg, "/settings/speech", model=v3).text.count("is not installed") >= 1


def test_speech_configured_dir_merges_into_its_models_row(pg, tmp_path):
    v3, en = list(hear.MODELS)
    d = install(tmp_path / "elsewhere", v3)
    pg.st.asr_model_dir = str(d)
    r = pg.c.get("/settings")
    assert r.text.count('type="radio"') == 2 and r.text.count("In use") == 1
    assert f"Folder: {d}" in r.text
    assert "Parakeet v3, recommended" in r.text
    assert r.text.count("Not installed. Download it with zeroout setup.") == 1  # only the 110M
    assert post(pg, "/settings/speech", model=v3).text.count("Saved") == 1
    assert pg.vals()["ASR_MODEL_DIR"] == str(d)


def test_speech_in_use_row_is_checked_and_folder_only_shown_there(pg):
    v3, en = list(hear.MODELS)
    from zeroout import home
    d = install(home.models_dir(), v3)
    install(home.models_dir(), en)
    pg.st.asr_model_dir = str(d)
    r = pg.c.get("/settings")
    assert r.text.count("Folder:") == 1 and str(d) in r.text


def test_settings_path_sits_under_the_lede_and_wraps(pg):
    r = pg.c.get("/settings")
    assert re.search(r'<p class="lede">[^<]*</p>', r.text) and str(pg.file) not in re.search(r'<p class="lede">[^<]*</p>', r.text).group(0)
    assert re.search(r'<p class="hint wrap">Saved in ' + re.escape(str(pg.file)) + "</p>", r.text)


def test_use_this_model_button_follows_the_list_directly(pg):
    r = pg.c.get("/settings")
    assert re.search(r'</ul>\s*<div class="actions"><button class="btn primary" type="submit">Use this model</button>', r.text)


def test_speech_choice_is_verified_written_applied_and_old_model_released(pg):
    v3, en = list(hear.MODELS)
    from zeroout import home
    install(home.models_dir(), v3)
    d = install(home.models_dir(), en)
    released = []
    pg.mp.setattr(hear, "release_except", released.append)
    r = post(pg, "/settings/speech", model=en)
    assert "Saved" in r.text
    assert pg.checked_models == [str(d)]
    assert pg.vals()["ASR_MODEL_DIR"] == str(d) and pg.st.asr_model_dir == str(d)
    assert released == [str(d)]
    assert pg.vals()["KEEP_ME"] == "kept"


def test_speech_unknown_or_traversing_choice_is_refused(pg):
    for bad in ("../../etc", "not-listed", "", "/tmp"):
        r = post(pg, "/settings/speech", model=bad)
        assert "Choose one of the models listed" in r.text or "is not installed" in r.text
    assert "ASR_MODEL_DIR" not in pg.vals() or not pg.vals()["ASR_MODEL_DIR"]


def test_speech_model_that_fails_its_check_is_refused(pg):
    v3, _ = list(hear.MODELS)
    from zeroout import home
    install(home.models_dir(), v3)

    def bad(path):
        raise RuntimeError("does not match the pinned hash; download the model again.")

    pg.services.verify_model = bad
    r = post(pg, "/settings/speech", model=v3)
    assert "pinned hash" in r.text and "Nothing was saved" in r.text
    assert not pg.vals().get("ASR_MODEL_DIR")


def test_hear_release_except_keeps_only_one(pg):
    hear._cache.clear()
    hear._cache.update({"a": 1, "b": 2})
    hear.release_except("b")
    assert hear._cache == {"b": 2}
    hear._cache.clear()


# ---------- spending ----------

def test_spending_saves_and_applies(pg):
    r = post(pg, "/settings/spending", cap="7.5")
    assert "Saved" in r.text
    assert pg.vals()["DAILY_CAP_USD"] == "7.5" and pg.st.daily_cap_usd == 7.5
    assert pg.vals()["KEEP_ME"] == "kept"


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "abc", "", "1\n2"])
def test_spending_must_be_a_finite_number_above_zero(pg, raw):
    r = post(pg, "/settings/spending", cap=raw)
    assert "The daily cap must be a number above 0, like 5." in r.text or "line break" in r.text
    assert pg.vals()["DAILY_CAP_USD"] == "5" and pg.st.daily_cap_usd == 5.0


# ---------- community ----------

def test_community_saves_and_applies(pg):
    r = post(pg, "/settings/community", repo="octo/zero-out-trees")
    assert "Saved" in r.text and pg.vals()["COMMUNITY_REPO"] == "octo/zero-out-trees"
    assert pg.st.community_repo == "octo/zero-out-trees"


@pytest.mark.parametrize("repo", ["", "nope", "a/..", "a/b/c", "a/../b", "-x/y z", "a/b\nc"])
def test_community_repo_must_be_owner_slash_name(pg, repo):
    before = pg.st.community_repo
    r = post(pg, "/settings/community", repo=repo)
    assert "owner/name" in r.text or "line break" in r.text
    assert "COMMUNITY_REPO" not in pg.vals() and pg.st.community_repo == before


def test_community_account_shows_the_github_login(pg):
    r = pg.c.get("/settings/community/account", headers=HX)
    assert "octo-cat" in r.text
    pg.services.github_login = lambda: None
    r = pg.c.get("/settings/community/account", headers=HX)
    assert "not signed in" in r.text.lower() or "no github account" in r.text.lower()


def test_page_loads_the_github_account_lazily(pg):
    calls = []
    pg.services.github_login = lambda: calls.append(1) or "octo"
    r = pg.c.get("/settings")
    assert calls == [] and 'hx-get="/settings/community/account"' in r.text and 'hx-trigger="load"' in r.text


# ---------- changes take effect without a restart ----------

def test_connect_after_saving_sees_the_new_number(pg):
    pg.c.post("/settings/phone", data={"sid": SID, "token": TOKEN_SECRET, "number": NUMBER}, headers=HX)
    r = pg.c.post(f"/c/{pg.cid}/connect", data={}, headers=ORIGIN)
    assert r.status_code == 200 and pg.connect_seen == [(NUMBER, NUMBER, NUMBER)]
    post(pg, "/settings/phone", sid=SID, token="", number="415 555 0188")
    pg.c.post(f"/c/{pg.cid}/connect", data={}, headers=ORIGIN)
    assert pg.connect_seen[-1] == ("+14155550188",) * 3


def test_crawl_after_saving_sees_the_new_budget(pg):
    pg.conn.execute("INSERT INTO runs (company_id, kind, started_at, cost_usd) VALUES (?, 'crawl', ?, 3.0)",
                    (pg.cid, datetime.now(timezone.utc).isoformat()))
    pg.conn.commit()
    pg.mp.setattr(web, "crawl", lambda *a, **k: None)
    assert pg.c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False).status_code == 303
    post(pg, "/settings/spending", cap="2")
    r = pg.c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 503 and web.BUDGET_MSG in r.text


def test_crawl_after_saving_loads_the_new_speech_model(pg):
    v3, en = list(hear.MODELS)
    from zeroout import home
    install(home.models_dir(), v3)
    d = install(home.models_dir(), en)
    loaded = []
    pg.mp.setattr(hear, "load", loaded.append)
    pg.mp.setattr(hear, "release_except", lambda d: None)
    pg.mp.setattr(web, "crawl", lambda *a, **k: None)
    c = pg.build(listen=None)
    c.post("/settings/speech", data={"model": en}, headers=HX)
    # no injected listen: the route checks the speech model before the first paid call
    c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False)
    assert loaded == [str(d)]


def test_crawl_rebuilds_the_twilio_probe_when_credentials_change(pg):
    built = []
    import zeroout.probe as probe_mod
    pg.mp.setattr(probe_mod, "twilio_probe", lambda s: built.append((s.twilio_sid, s.twilio_token)) or pg.menu.probe)
    pg.mp.setattr(web, "crawl", lambda *a, **k: None)
    c = pg.build(probe=None)
    refused = c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN)
    assert refused.status_code == 400 and "Needs Twilio" in refused.text and built == []
    c.post("/settings/phone", data={"sid": SID, "token": TOKEN_SECRET, "number": NUMBER}, headers=HX)
    c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False)
    c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False)
    assert built == [(SID, TOKEN_SECRET)]


def test_twilio_verify_rebuilds_its_client_when_credentials_change(pg, monkeypatch):
    made = []

    class Client:
        def __init__(self, sid, token):
            made.append((sid, token))
            self.verify = SimpleNamespace(v2=SimpleNamespace(services=lambda s: SimpleNamespace(
                verifications=SimpleNamespace(create=lambda **k: None))))

    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", Client)
    st = dataclasses.replace(pg.st, twilio_sid="ACold", twilio_token="t1", twilio_verify_sid="VA1")
    v = web.TwilioVerify(st)
    v.start("+14155550100")
    v.start("+14155550100")
    st.twilio_token = "t2"
    v.start("+14155550100")
    assert made == [("ACold", "t1"), ("ACold", "t2")]


def test_twilio_verify_keeps_an_injected_client(pg):
    class C:
        verify = SimpleNamespace(v2=SimpleNamespace(services=lambda s: SimpleNamespace(
            verifications=SimpleNamespace(create=lambda **k: None))))

    st = dataclasses.replace(pg.st, twilio_token="t1")
    v = web.TwilioVerify(st, C())
    st.twilio_token = "t2"
    v.start("+14155550100")  # would try the real Client if it rebuilt


def test_failed_reload_is_reported_and_leaves_running_settings_alone(pg):
    pg.mp.setattr(config, "load_settings", lambda: (_ for _ in ()).throw(ValueError("bad file")))
    r = post(pg, "/settings/spending", cap="9")
    assert "Saved to" in r.text and "restart zeroout run" in r.text and "bad file" in r.text
    assert pg.st.daily_cap_usd == 5.0


def test_save_writes_to_home_file_when_named_file_is_missing(pg, tmp_path):
    from zeroout import home
    pg.mp.setenv("ZEROOUT_ENV_FILE", str(tmp_path / "missing.env"))
    post(pg, "/settings/spending", cap="3")
    assert dotenv_values(home.config_file())["DAILY_CAP_USD"] == "3"
    assert not (tmp_path / "missing.env").exists()


# ---------- setup's shared checks, called without an Env ----------

def test_setup_checks_run_without_an_env():
    from zeroout import setup
    assert setup.list_models(Http(models_response(IDS)), "https://tf.example/v1", KEY) == IDS
    assert setup.nemotron_ids(IDS) == [NANO, SUPER]
    assert setup.base_problem("https://tf.example") == "" and "Got http://tf.example" in setup.base_problem("http://tf.example")
    assert setup.sid_problem(SID) == "" and "Got 4 characters" in setup.sid_problem("AC12")
    assert setup.cap_problem("5") == "" and "Got nan" in setup.cap_problem("nan")
    t = FakeTwilio()
    client, account = setup.fetch_account(lambda sid, token: t, SID, "tok")
    assert client is t and account.type == "Full"
    assert setup.request_validation(t, NUMBER) == "123456"
    assert t.created == [{"phone_number": NUMBER, "friendly_name": "Zero-Out"}]


# ---------- stop using Twilio ----------

TWILIO_KEYS = ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_VERIFY_SID", "OWNER_NUMBER")


def with_twilio(pg):
    post(pg, "/settings/phone", sid=SID, token=TOKEN_SECRET, number=NUMBER)
    text = pg.file.read_text()
    pg.file.write_text(text.replace("TWILIO_VERIFY_SID=\n", "TWILIO_VERIFY_SID=VA" + "1" * 32 + "\n"))
    pg.st.twilio_verify_sid = "VA" + "1" * 32
    pg.clock.t = 1000  # the save remembered its own check; let that answer age out


def test_stop_clears_the_keys_keeps_the_rest_and_updates_the_running_app(pg):
    with_twilio(pg)
    assert pg.vals()["TWILIO_VERIFY_SID"].startswith("VA")
    r = post(pg, "/settings/phone/stop")
    assert r.status_code == 200 and "Saved" in r.text
    assert "Mapping with your own phone on speaker still works without Twilio" in r.text
    no_secrets(r, TOKEN_SECRET)
    v = pg.vals()
    assert all(not v.get(k) for k in TWILIO_KEYS)
    assert v["KEEP_ME"] == "kept" and v["TF_API_KEY"] == OLD_KEY and v["SESSION_SECRET"] == SESSION
    st = pg.st
    assert (st.twilio_sid, st.twilio_token, st.twilio_from, st.twilio_verify_sid, st.owner_number) == ("",) * 5
    assert "Stop using Twilio" not in pg.c.get("/settings").text


def test_stop_button_only_shows_while_twilio_is_set(pg):
    assert "Stop using Twilio" not in pg.c.get("/settings").text
    with_twilio(pg)
    page = pg.c.get("/settings")
    assert "Stop using Twilio" in page.text and 'hx-post="/settings/phone/stop"' in page.text


@pytest.mark.parametrize("key", TWILIO_KEYS)
def test_stop_is_refused_when_a_key_is_set_in_the_environment(pg, key):
    with_twilio(pg)
    before = pg.file.read_text()
    pg.mp.setenv(key, "from-env-1234")
    r = post(pg, "/settings/phone/stop")
    assert f"Set in your environment as {key}; change it there." in r.text
    assert pg.file.read_text() == before and pg.st.twilio_sid == SID
    no_secrets(r, TOKEN_SECRET, "from-env-1234")


def test_after_stop_connect_and_the_company_page_take_the_needs_twilio_path(pg):
    with_twilio(pg)
    assert "We call you at" in pg.c.get(f"/c/{pg.cid}").text
    post(pg, "/settings/phone/stop")
    assert "Connect me needs Twilio" in pg.c.get(f"/c/{pg.cid}").text
    r = pg.c.post(f"/c/{pg.cid}/connect", data={}, headers=ORIGIN)
    assert r.status_code == 403 and "Verify your phone first" in r.text and pg.connect_seen == []


def test_stop_drops_the_cached_twilio_clients_and_any_verification(pg):
    built = []
    import zeroout.probe as probe_mod
    pg.mp.setattr(probe_mod, "twilio_probe", lambda s: built.append((s.twilio_sid, s.twilio_token)) or pg.menu.probe)
    pg.mp.setattr(web, "crawl", lambda *a, **k: None)
    c = pg.build(probe=None)
    c.post("/settings/phone", data={"sid": SID, "token": TOKEN_SECRET, "number": NUMBER}, headers=HX)
    c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN, follow_redirects=False)
    c.post("/settings/phone/verify", headers=HX)
    r = c.post("/settings/phone/stop", headers=HX)
    assert "every 3s" not in r.text
    assert "every 3s" not in c.post("/settings/phone/verify/status", headers=HX).text
    after = c.post(f"/c/{pg.cid}/crawl", data={}, headers=ORIGIN)
    assert after.status_code == 400 and "Needs Twilio" in after.text
    assert built == [(SID, TOKEN_SECRET)]


# ---------- the saved number's state, checked lazily ----------

def number(pg):
    return pg.c.get("/settings/phone/number", headers=HX)


def test_page_checks_the_number_lazily_not_on_load(pg):
    with_twilio(pg)
    pg.twilio_calls.clear()
    r = pg.c.get("/settings")
    assert pg.twilio_calls == []
    assert 'hx-get="/settings/phone/number"' in r.text and 'hx-trigger="load"' in r.text
    assert "Not checked yet" not in r.text


def test_number_state_without_a_saved_number_is_empty(pg):
    r = number(pg)
    assert r.status_code == 200 and "state" not in r.text and pg.twilio_calls == []


def test_number_state_verified(pg):
    with_twilio(pg)
    pg.twilio = FakeTwilio(caller_ids=([SimpleNamespace()],))
    r = number(pg)
    assert 'class="state on"' in r.text and "Verified" in r.text and "Verify my number" not in r.text


def test_number_state_not_verified_offers_the_verify_button(pg):
    with_twilio(pg)
    r = number(pg)
    assert 'class="state warn"' in r.text and "Not verified yet" in r.text
    assert "Verify my number" in r.text and 'hx-post="/settings/phone/verify"' in r.text


def test_number_state_when_twilio_cannot_be_reached_names_the_reason_without_secrets(pg):
    from twilio.base.exceptions import TwilioException
    with_twilio(pg)

    class Boom(FakeTwilio):
        def __init__(self):
            super().__init__()
            self.outgoing_caller_ids = SimpleNamespace(
                list=lambda **k: (_ for _ in ()).throw(TwilioException(f"down {TOKEN_SECRET}")))

    pg.twilio = Boom()
    r = number(pg)
    assert 'class="state off"' in r.text and "Could not check with Twilio" in r.text
    no_secrets(r, TOKEN_SECRET)
    pg.twilio = FakeTwilio()
    assert "Not verified yet" in number(pg).text  # an error is not cached


def test_number_state_is_cached_for_sixty_seconds(pg):
    with_twilio(pg)
    pg.twilio_calls.clear()
    number(pg)
    number(pg)
    pg.clock.t = 1059
    number(pg)
    assert len(pg.twilio_calls) == 1
    pg.clock.t = 1061
    number(pg)
    assert len(pg.twilio_calls) == 2


def test_finishing_a_verification_updates_the_cached_state(pg):
    with_twilio(pg)
    assert "Not verified yet" in number(pg).text
    post(pg, "/settings/phone/verify")
    pg.twilio.outgoing_caller_ids.results = [[SimpleNamespace()]]
    pg.c.post("/settings/phone/verify/status", headers=HX)
    assert "Verified" in number(pg).text and "Not verified yet" not in number(pg).text


# ---------- security review ----------

def test_the_key_never_follows_a_redirect():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from zeroout import setup
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class First(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal?x=1")
            self.end_headers()

        def log_message(self, *a):
            pass

    first = HTTPServer(("127.0.0.1", 0), First)
    for srv in (first, target):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(setup.TfError) as e:
            setup.list_models(setup.open_no_redirect, f"http://localhost:{first.server_port}/v1", KEY)
    finally:
        first.shutdown()
        target.shutdown()
    assert seen == []
    assert str(e.value) == (f"Token Factory redirected to http://127.0.0.1:{target.server_port}; "
                            "use that address as the base URL.")
    assert "steal" not in str(e.value) and KEY not in str(e.value)


def test_settings_and_setup_use_the_redirect_refusing_opener(pg, tmp_path, monkeypatch):
    from zeroout import setup
    assert settings_page.Services().http is setup.open_no_redirect
    seen = []
    monkeypatch.setattr(setup, "run", lambda env: seen.append(env) or 0)
    setup.main(["--home", str(tmp_path / "h")])
    assert seen[0].http is setup.open_no_redirect


def test_a_redirect_is_shown_as_a_sentence_in_settings(pg):
    from zeroout import setup
    pg.http = lambda *a, **k: (_ for _ in ()).throw(
        setup.TfError("Token Factory redirected to http://evil.example; use that address as the base URL."))
    r = post(pg, "/settings/model", api_key=KEY, base_url="", nano=NANO, super=SUPER)
    assert "Token Factory redirected to http://evil.example" in r.text and "Nothing was saved" in r.text
    no_secrets(r, KEY)


def test_stop_blanks_the_twilio_keys_in_every_file_that_sets_them(pg):
    from zeroout import home
    with_twilio(pg)
    other = home.config_file()
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(f"TWILIO_SID={SID}\nTWILIO_TOKEN={TOKEN_SECRET}\nOWNER_NUMBER={NUMBER}\nOTHER=kept\n")
    r = post(pg, "/settings/phone/stop")
    assert "Saved" in r.text and str(pg.file) in r.text and str(other) in r.text
    no_secrets(r, TOKEN_SECRET)
    for f in (pg.file, other):
        v = dotenv_values(f, interpolate=False)
        assert all(not v.get(k) for k in TWILIO_KEYS)
    assert dotenv_values(other)["OTHER"] == "kept" and pg.vals()["KEEP_ME"] == "kept"
    assert all(not getattr(pg.st, a) for a in ("twilio_sid", "twilio_token", "twilio_from", "twilio_verify_sid", "owner_number"))


def test_stop_does_not_claim_success_when_a_key_comes_back_on_reload(pg):
    from zeroout import home
    with_twilio(pg)
    other = home.config_file()
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(f"TWILIO_SID={SID}\nTWILIO_TOKEN={TOKEN_SECRET}\n")
    pg.mp.setattr(settings_page, "twilio_files", lambda: [pg.file])
    r = post(pg, "/settings/phone/stop")
    assert "Saved. Twilio is off" not in r.text and "notice strong" in r.text
    assert "still set" in r.text and str(other) in r.text
    no_secrets(r, TOKEN_SECRET)


@pytest.mark.parametrize("base", ["https://[::1/v1", "https://[", "http://[::1"])
def test_unparseable_base_urls_are_a_sentence_not_a_500(pg, base):
    from zeroout import setup
    assert setup._base_ok(base) is False and "The base URL must start with https://" in setup.base_problem(base)
    r = post(pg, "/settings/model", api_key=KEY, base_url=base, nano=NANO, super=SUPER)
    assert r.status_code == 200 and "The base URL must start with https://" in r.text


def test_a_base_url_with_a_bad_port_is_a_sentence_not_a_500(pg):
    from zeroout import setup
    with pytest.raises(setup.TfError) as e:
        setup.list_models(setup.open_no_redirect, "https://h.invalid:abc/v1", KEY)
    assert "h.invalid:abc" in str(e.value) and KEY not in str(e.value)
    import http.client
    pg.http = lambda *a, **k: (_ for _ in ()).throw(http.client.InvalidURL("nonnumeric port: 'abc'"))
    r = post(pg, "/settings/model", api_key=KEY, base_url="https://h.invalid:abc/v1", nano=NANO, super=SUPER)
    assert r.status_code == 200 and "not an address Zero-Out can use" in r.text
    no_secrets(r, KEY)


def test_background_reads_have_their_own_generous_limiter(pg):
    for _ in range(settings_page.CHECKS_PER_HOUR + 5):
        assert pg.c.get("/settings/community/account", headers=HX).status_code == 200
    r = post(pg, "/settings/spending", cap="6")
    assert "Saved" in r.text and "Too many" not in r.text
    for _ in range(settings_page.READS_PER_HOUR):
        pg.c.get("/settings/community/account", headers=HX)
    assert "Too many" in pg.c.get("/settings/community/account", headers=HX).text


@pytest.mark.parametrize("path", ["/settings", "/settings/phone/number", "/settings/community/account"])
def test_cross_site_gets_are_refused(pg, path):
    r = pg.c.get(path, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and "another site" in r.text
    for ok in ("same-origin", "same-site", "none"):
        assert pg.c.get(path, headers={"Sec-Fetch-Site": ok}).status_code == 200
    assert pg.c.get(path).status_code == 200


def test_the_verify_poll_is_a_post_so_the_origin_check_covers_it(pg):
    saved_phone(pg)
    r = post(pg, "/settings/phone/verify")
    assert 'hx-post="/settings/phone/verify/status"' in r.text and 'hx-trigger="every 3s"' in r.text
    assert 'hx-get="/settings/phone/verify/status"' not in r.text
    assert pg.c.get("/settings/phone/verify/status").status_code == 405
    foreign = pg.c.post("/settings/phone/verify/status", headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403


# ---------- symlinked config, partial writes ----------

def test_a_symlinked_config_is_written_through_and_stays_a_link(pg, tmp_path):
    real = tmp_path / "dotfiles" / "real.env"
    write_env(real)
    link = tmp_path / "cfg" / "link.env"
    link.symlink_to(real)
    pg.mp.setenv("ZEROOUT_ENV_FILE", str(link))
    r = post(pg, "/settings/spending", cap="8")
    assert "Saved" in r.text
    assert link.is_symlink() and dotenv_values(real, interpolate=False)["DAILY_CAP_USD"] == "8"
    assert dotenv_values(real, interpolate=False)["KEEP_ME"] == "kept"
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o600


def test_stop_through_a_symlinked_config_clears_the_target(pg, tmp_path):
    with_twilio(pg)
    real = tmp_path / "dotfiles" / "real.env"
    real.parent.mkdir()
    pg.file.rename(real)
    pg.file.symlink_to(real)
    r = post(pg, "/settings/phone/stop")
    assert "Saved. Twilio is off" in r.text
    assert pg.file.is_symlink() and not dotenv_values(real, interpolate=False).get("TWILIO_SID")


def test_setup_write_config_follows_a_symlink(tmp_path):
    from zeroout import setup
    real = tmp_path / "real.env"
    real.write_text("A=1\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    setup.write_config(link, "A=2\n")
    assert link.is_symlink() and real.read_text() == "A=2\n"


def test_stop_reports_a_partial_write_exactly_and_reloads(pg):
    from zeroout import home, setup
    with_twilio(pg)
    other = home.config_file()
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(f"TWILIO_SID={SID}\nTWILIO_TOKEN={TOKEN_SECRET}\nOWNER_NUMBER={NUMBER}\n")
    real_write = setup.write_config

    def flaky(path, text):
        if os.path.realpath(path) == os.path.realpath(other):
            raise PermissionError(13, "Permission denied")
        real_write(path, text)

    pg.mp.setattr(setup, "write_config", flaky)
    r = post(pg, "/settings/phone/stop")
    assert "Nothing was saved" not in r.text and "Saved. Twilio is off" not in r.text
    assert f"Changed {pg.file}" in r.text and f"Could not write {other}: Permission denied" in r.text
    no_secrets(r, TOKEN_SECRET)
    assert not pg.vals().get("TWILIO_SID") and dotenv_values(other)["TWILIO_SID"] == SID
    assert pg.st.twilio_sid == SID  # reloaded: the second file still supplies it


def test_owner_number_row_is_shown_the_way_people_write_it(pg):
    with_twilio(pg)
    t = pg.c.get("/settings").text
    assert f'<span class="grow">{web.tel(NUMBER)}<' in t and f'grow">{NUMBER}<' not in t
