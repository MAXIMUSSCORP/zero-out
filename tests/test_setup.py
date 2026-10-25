import io
import json
import os
import stat
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from twilio.base.exceptions import TwilioRestException

from zeroout import setup as S

KEY = "tfkey-abcdefgh1234"
SID = "AC" + "a1" * 16
NANO, SUPER = S.DEFAULT_NANO, S.DEFAULT_SUPER
V3, EN = (m for m, _ in S.SPEECH_MODELS)
IDS = [NANO, SUPER, "nvidia/other"]


class Script:
    """Scripted answers; a KeyboardInterrupt or exception instance in the list is raised."""

    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"script ran out at {prompt!r}")
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def models_response(ids):
    return SimpleNamespace(read=lambda: json.dumps({"data": [{"id": i} for i in ids]}).encode(), close=lambda: None)


class Http:
    def __init__(self, *results):
        self.results, self.requests = list(results), []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, BaseException):
            raise r
        return r


def refused(code=401):
    return urllib.error.HTTPError("u", code, "no", {}, io.BytesIO())


class Lister:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def list(self, phone_number=None):
        self.calls.append(phone_number)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class FakeTwilio:
    def __init__(self, acct_type="Full", caller_ids=([],), incoming=([],), fetch_error=None):
        self.fetch_error, self.acct_type = fetch_error, acct_type
        self.outgoing_caller_ids = Lister(*caller_ids)
        self.incoming_phone_numbers = Lister(*incoming)
        self.created = []
        outer = self

        class Accounts:
            def __call__(self, sid):
                return SimpleNamespace(fetch=outer._fetch)

        self.api = SimpleNamespace(v2010=SimpleNamespace(accounts=Accounts()))
        self.validation_requests = SimpleNamespace(create=self._create)

    def _fetch(self):
        if self.fetch_error:
            raise self.fetch_error
        return SimpleNamespace(type=self.acct_type)

    def _create(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(validation_code="123456")


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def build(tmp_path, answers, secrets=(), http=None, twilio=None, verified=False, **kw):
    clock = Clock()
    lines = []
    installs = []

    def install(name, root, progress=None, **_):
        installs.append(name)
        (Path(root) / name).mkdir(parents=True, exist_ok=True)
        progress(50, 100)
        progress(100, 100)
        return Path(root) / name

    def verify(path):
        if not verified:
            raise RuntimeError("missing")

    env = S.Env(
        ask=Script(answers),
        secret=Script(secrets),
        out=lines.append,
        http=http or Http(models_response(IDS)),
        twilio=lambda sid, token: twilio,
        install=install,
        verify=verify,
        config_path=tmp_path / "cfg" / "zeroout.env",
        models_dir=tmp_path / "models",
        sleep=clock.sleep,
        clock=clock.now,
        new_secret=lambda: "N" * 48,
        **kw,
    )
    env.lines, env.installs, env.clock_obj = lines, installs, clock
    return env


def text(env):
    return "\n".join(env.lines)


def saved(env):
    assert not env.ask.answers and not env.secret.answers, "script not fully consumed"
    return dotenv_values(env.config_path, interpolate=False)


def test_happy_path_without_twilio(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY])  # base, model 1, no Twilio, cap
    assert S.run(env) == 0
    v = saved(env)
    assert v["TF_API_KEY"] == KEY and v["TF_BASE_URL"] == S.DEFAULT_BASE
    assert v["MODEL_NANO"] == NANO and v["MODEL_SUPER"] == SUPER
    assert v["ASR_MODEL_DIR"] == str(tmp_path / "models" / V3)
    assert v["TWILIO_SID"] == v["TWILIO_TOKEN"] == v["TWILIO_FROM"] == v["OWNER_NUMBER"] == ""
    assert v["DAILY_CAP_USD"] == "5" and v["RATE_PER_MIN"] == "0.014" and v["COOKIE_SECURE"] == "0"
    assert v["SESSION_SECRET"] == "N" * 48
    assert env.installs == [V3]
    assert stat.S_IMODE(env.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.config_path.parent.stat().st_mode) == 0o700
    assert not list(env.config_path.parent.glob("*.tmp"))
    req, timeout = env.http.requests[0]
    assert req.full_url == S.DEFAULT_BASE + "models" and timeout == 20
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    assert "  saved     " in text(env) and "  next      zeroout run" in text(env)
    assert KEY not in text(env)


def test_twilio_with_verified_number(tmp_path):
    tw = FakeTwilio(caller_ids=([{"x": 1}],))
    env = build(tmp_path, ["", "", "y", SID, "(780) 555-0142", ""], secrets=[KEY, "tok123456789"], twilio=tw)
    assert S.run(env) == 0
    v = saved(env)
    assert (v["TWILIO_SID"], v["TWILIO_TOKEN"]) == (SID, "tok123456789")
    assert v["TWILIO_FROM"] == v["OWNER_NUMBER"] == "+17805550142"
    assert v["TWILIO_VERIFY_SID"] == ""
    assert tw.created == [] and "Trial" not in text(env)


def test_incoming_number_counts_as_ready(tmp_path):
    tw = FakeTwilio(incoming=([{"x": 1}],))
    env = build(tmp_path, ["", "", "y", SID, "7805550142", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0 and tw.created == []


def test_validation_succeeds_on_third_poll(tmp_path):
    tw = FakeTwilio(caller_ids=([], [], [], [{"x": 1}]))  # ready check, poll 1, poll 2, poll 3
    env = build(tmp_path, ["", "", "y", SID, "7805550142", "y", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0
    assert tw.created == [{"phone_number": "+17805550142", "friendly_name": "Zero-Out"}]
    assert "type this code on the keypad: 123456" in text(env)
    assert env.clock_obj.sleeps == [3, 3]
    assert "  verified  +17805550142" in text(env)


def test_validation_timeout_still_saves(tmp_path):
    tw = FakeTwilio()
    env = build(tmp_path, ["", "", "y", SID, "7805550142", "y", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0
    assert sum(env.clock_obj.sleeps) == 120
    assert "Verified Caller IDs" in text(env) and "run zeroout setup again" in text(env)
    assert saved(env)["OWNER_NUMBER"] == "+17805550142"


def test_trial_warning(tmp_path):
    tw = FakeTwilio(acct_type="Trial", caller_ids=([{"x": 1}],))
    env = build(tmp_path, ["", "", "y", SID, "7805550142", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0
    assert S.TRIAL_WARNING in text(env)
    assert saved(env)["TWILIO_SID"] == SID


def test_twilio_bad_credentials_then_retry(tmp_path):
    bad = TwilioRestException(401, "uri", "Authenticate")
    clients = [FakeTwilio(fetch_error=bad), FakeTwilio(caller_ids=([{"x": 1}],))]
    env = build(tmp_path, ["", "", "y", SID, "y", SID, "7805550142", ""], secrets=[KEY, "bad", "good"])
    env.twilio = lambda sid, token: clients.pop(0)
    assert S.run(env) == 0
    assert "refused the Account SID or auth token" in text(env)
    assert saved(env)["TWILIO_TOKEN"] == "good"


def test_bad_sid_and_phone_are_reasked(tmp_path):
    tw = FakeTwilio(caller_ids=([{"x": 1}],))
    env = build(tmp_path, ["", "", "y", "ACnope", SID, "123", "7805550142", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0
    assert "Got 6 characters" in text(env) and "Enter a US number" in text(env)


def test_refused_key_then_retry(tmp_path):
    http = Http(refused(401), models_response(IDS))
    env = build(tmp_path, ["", "y", "", "", "n", ""], secrets=["bad", KEY], http=http)
    assert S.run(env) == 0
    assert "refused the key (HTTP 401)" in text(env)
    assert saved(env)["TF_API_KEY"] == KEY


def test_refused_key_then_quit_writes_nothing(tmp_path):
    env = build(tmp_path, ["", "n"], secrets=["bad"], http=Http(refused(403)))
    assert S.run(env) == 1
    assert "Setup cancelled, nothing was saved." in text(env)
    assert not env.config_path.exists()


def test_network_error_is_named(tmp_path):
    env = build(tmp_path, ["", "n"], secrets=[KEY], http=Http(urllib.error.URLError("no route")))
    assert S.run(env) == 1
    assert "Could not reach Token Factory" in text(env) and "no route" in text(env)


def test_missing_model_offers_closest_nemotron(tmp_path):
    ids = [SUPER, "nvidia/nemotron-3-nano-30b-a3b", "nvidia/nemotron-4-340b", "meta/llama"]
    env = build(tmp_path, ["", "1", "", "n", ""], secrets=[KEY], http=Http(models_response(ids)))
    assert S.run(env) == 0
    out = text(env)
    assert f"Nano model {NANO} is not in your Token Factory list" in out
    assert "nemotron-3-nano-30b-a3b" in out and "meta/llama" not in out
    assert saved(env)["MODEL_NANO"] == "nvidia/nemotron-3-nano-30b-a3b"


def test_no_nemotron_listed_cancels(tmp_path):
    env = build(tmp_path, [""], secrets=[KEY], http=Http(models_response(["meta/llama"])))
    assert S.run(env) == 1 and not env.config_path.exists()


def test_rerun_keeps_existing_values(tmp_path):
    first = build(tmp_path, ["", "", "n", "7.5"], secrets=[KEY])
    assert S.run(first) == 0
    before = saved(first)
    second = build(tmp_path, ["", "", "", ""], secrets=[""], verified=True)
    assert S.run(second) == 0
    assert "[****1234]" in second.secret.prompts[0] and f"[{S.DEFAULT_BASE}]" in second.ask.prompts[0]
    assert "[7.5]" in second.ask.prompts[-1]
    after = saved(second)
    assert after == before and after["SESSION_SECRET"] == "N" * 48
    assert second.installs == []  # already installed
    assert KEY not in text(second)


def test_rerun_keeps_foreign_keys(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY])
    env.config_path.parent.mkdir(parents=True)
    env.config_path.write_text("DB_PATH=/x/zeroout.db\nSESSION_SECRET=" + "k" * 40 + "\n")
    assert S.run(env) == 0
    v = saved(env)
    assert v["DB_PATH"] == "/x/zeroout.db" and v["SESSION_SECRET"] == "k" * 40


def test_ctrl_c_writes_nothing(tmp_path):
    env = build(tmp_path, ["", "", KeyboardInterrupt()], secrets=[KEY])
    assert S.run(env) == 1
    assert "Setup cancelled, nothing was saved." in text(env)
    assert not env.config_path.exists() and not env.config_path.parent.exists()


def test_ctrl_c_at_secret_prompt(tmp_path):
    env = build(tmp_path, [], secrets=[KeyboardInterrupt()])
    assert S.run(env) == 1 and not env.config_path.exists()


def test_line_break_in_answer_is_refused_and_reasked(tmp_path):
    env = build(tmp_path, ["https://x/v1\nEVIL=1", "", "", "n", ""], secrets=[KEY])
    assert S.run(env) == 0
    assert "cannot contain a line break" in text(env)
    assert "EVIL" not in env.config_path.read_text()


def test_render_refuses_newlines():
    with pytest.raises(ValueError, match="MODEL_NANO"):
        S.render({"MODEL_NANO": "a\nb"})
    with pytest.raises(ValueError):
        S.render({"TF_API_KEY": "a\rb"})


def test_quoting_round_trips_through_dotenv(tmp_path):
    tricky = {"TF_API_KEY": "it's a # \"test\" \\ $HOME", "MODEL_NANO": "", "MODEL_SUPER": "plain/value-1.2"}
    p = tmp_path / "x.env"
    S.write_config(p, S.render(tricky))
    got = dotenv_values(p)
    assert all(got[k] == v for k, v in tricky.items())


def test_model_skipped_when_verified(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], verified=True)
    (tmp_path / "models" / V3).mkdir(parents=True)
    assert S.run(env) == 0
    assert env.installs == [] and "already installed" in text(env)


def test_english_model_choice(tmp_path):
    env = build(tmp_path, ["", "2", "n", ""], secrets=[KEY])
    assert S.run(env) == 0 and env.installs == [EN]
    assert saved(env)["ASR_MODEL_DIR"].endswith(EN) and os.path.isabs(saved(env)["ASR_MODEL_DIR"])
    assert "  download  50%" in text(env) and "  download  100%" in text(env)


def test_skip_model_flag(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], skip_model=True)
    assert S.run(env) == 0 and env.installs == []
    assert saved(env)["ASR_MODEL_DIR"] == str(tmp_path / "models" / V3)


def test_install_failure_message_then_retry(tmp_path):
    env = build(tmp_path, ["", "", "y", "n", ""], secrets=[KEY])
    real, calls = env.install, []

    def flaky(name, root, **kw):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("The archive hash did not match.")
        return real(name, root, **kw)

    env.install = flaky
    assert S.run(env) == 0 and len(calls) == 2
    assert "The archive hash did not match." in text(env)


@pytest.mark.parametrize("cap", ["0", "-3", "abc", "nan", "inf"])
def test_bad_cap_is_reasked(tmp_path, cap):
    env = build(tmp_path, ["", "", "n", cap, "12"], secrets=[KEY])
    assert S.run(env) == 0
    assert f"Got {cap}." in text(env) and saved(env)["DAILY_CAP_USD"] == "12"


def test_unwritable_config_reports_and_exits_1(tmp_path):
    blocker = tmp_path / "cfg"
    blocker.write_text("a file, not a folder")
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY])
    assert S.run(env) == 1 and "nothing was saved" in text(env)


def test_write_config_replaces_atomically(tmp_path):
    p = tmp_path / "d" / "zeroout.env"
    S.write_config(p, "A=1\n")
    S.write_config(p, "A=2\n")
    assert p.read_text() == "A=2\n" and [x.name for x in p.parent.iterdir()] == ["zeroout.env"]
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_mask():
    assert S.mask("abcdefgh") == "****efgh" and S.mask("ab") == "****" and S.mask("") == ""


def preset(env, **values):
    env.config_path.parent.mkdir(parents=True, exist_ok=True)
    env.config_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def test_download_network_error_is_named_and_retried(tmp_path):
    env = build(tmp_path, ["", "", "y", "n", ""], secrets=[KEY])
    real, calls = env.install, []

    def flaky(name, root, **kw):
        calls.append(name)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return real(name, root, **kw)

    env.install = flaky
    assert S.run(env) == 0 and len(calls) == 2
    assert "The download stopped: connection reset. Check your connection." in text(env)


def test_twilio_offline_rerun_keeps_saved_settings(tmp_path):
    tw = FakeTwilio(fetch_error=OSError("network down"))
    env = build(tmp_path, ["", "", "", "", "y", ""], secrets=["", ""], twilio=tw)
    preset(env, TWILIO_SID=SID, TWILIO_TOKEN="tok-saved-1234", TWILIO_FROM="+17805550142", OWNER_NUMBER="+17805550142",
           TF_API_KEY=KEY)
    assert S.run(env) == 0
    assert "Keep the saved Twilio settings without checking them?" in env.ask.prompts[-2]
    v = saved(env)
    assert (v["TWILIO_SID"], v["TWILIO_TOKEN"], v["OWNER_NUMBER"]) == (SID, "tok-saved-1234", "+17805550142")


def test_twilio_off_only_when_user_says_no(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY])
    preset(env, TWILIO_SID=SID, TWILIO_TOKEN="tok", OWNER_NUMBER="+17805550142")
    assert S.run(env) == 0 and saved(env)["TWILIO_SID"] == ""


def test_existing_model_elsewhere_is_kept(tmp_path):
    elsewhere = tmp_path / "mine" / V3
    elsewhere.mkdir(parents=True)
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], verified=True)
    preset(env, ASR_MODEL_DIR=elsewhere)
    assert S.run(env) == 0
    assert env.installs == [] and saved(env)["ASR_MODEL_DIR"] == str(elsewhere)


def test_skip_model_keeps_valid_existing_and_names_target(tmp_path):
    elsewhere = tmp_path / "mine" / V3
    elsewhere.mkdir(parents=True)
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], verified=True, skip_model=True)
    preset(env, ASR_MODEL_DIR=elsewhere)
    assert S.run(env) == 0 and saved(env)["ASR_MODEL_DIR"] == str(elsewhere)
    fresh = build(tmp_path / "b", ["", "", "n", ""], secrets=[KEY], skip_model=True)
    assert S.run(fresh) == 0
    assert f"copy the folder to {tmp_path / 'b' / 'models' / V3} yourself" in text(fresh)


def test_pending_line_after_timeout(tmp_path):
    tw = FakeTwilio()
    env = build(tmp_path, ["", "", "y", SID, "7805550142", "y", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0
    i = [n for n, l in enumerate(env.lines) if "  saved " in l][0]
    assert env.lines[i + 1] == "  pending   +17805550142 is not a verified caller ID yet; calls will fail until you finish verifying it"
    assert env.lines[i + 2].startswith("  next ")


def test_pending_line_when_user_declines_verification(tmp_path):
    tw = FakeTwilio()
    env = build(tmp_path, ["", "", "y", SID, "7805550142", "n", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0 and "  pending   +17805550142" in text(env)


def test_no_pending_line_when_verified(tmp_path):
    tw = FakeTwilio(caller_ids=([{"x": 1}],))
    env = build(tmp_path, ["", "", "y", SID, "7805550142", ""], secrets=[KEY, "t"], twilio=tw)
    assert S.run(env) == 0 and "  pending   " not in text(env)


def test_ctrl_c_during_poll_skips_waiting_and_saves(tmp_path):
    tw = FakeTwilio()
    env = build(tmp_path, ["", "", "y", SID, "7805550142", "y", ""], secrets=[KEY, "t"], twilio=tw)

    def interrupted(_):
        raise KeyboardInterrupt

    env.sleep = interrupted
    assert S.run(env) == 0
    assert "Stopped waiting" in text(env) and "  pending   +17805550142" in text(env)
    assert saved(env)["OWNER_NUMBER"] == "+17805550142"


@pytest.mark.parametrize("bad", ["http://api.example.com/v1/", "ftp://x/v1", "api.example.com/v1", "https://"])
def test_non_https_base_url_refused_before_sending_key(tmp_path, bad):
    env = build(tmp_path, [bad, "", "", "n", ""], secrets=[KEY])
    assert S.run(env) == 0
    assert "must start with https://" in text(env)
    assert len(env.http.requests) == 1 and env.http.requests[0][0].full_url.startswith(S.DEFAULT_BASE)


@pytest.mark.parametrize("local", ["http://localhost:8000/v1", "http://127.0.0.1/v1"])
def test_http_allowed_for_localhost(tmp_path, local):
    env = build(tmp_path, [local, "", "n", ""], secrets=[KEY])
    assert S.run(env) == 0 and saved(env)["TF_BASE_URL"] == local


def test_network_error_keeps_key_but_refusal_clears_it(tmp_path):
    env = build(tmp_path, ["", "y", "", "", "n", ""], secrets=[KEY, ""],
                http=Http(urllib.error.URLError("down"), models_response(IDS)))
    assert S.run(env) == 0
    assert f"[****{KEY[-4:]}]" in env.secret.prompts[1] and saved(env)["TF_API_KEY"] == KEY
    env = build(tmp_path / "b", ["", "y", "", "", "n", ""], secrets=[KEY, KEY],
                http=Http(refused(401), models_response(IDS)))
    assert S.run(env) == 0
    assert "[" not in env.secret.prompts[1]


def test_existing_file_is_not_interpolated(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY])
    preset(env, TF_API_KEY="x", DB_PATH="'${TF_API_KEY}/$HOME'", SESSION_SECRET="k" * 40)
    assert S.run(env) == 0
    assert saved(env)["DB_PATH"] == "${TF_API_KEY}/$HOME"


def test_folder_permissions_are_tightened_after_writing(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], data_dir=tmp_path / "data")
    env.config_path.parent.mkdir(mode=0o755)
    env.data_dir.mkdir(mode=0o755)
    os.chmod(env.config_path.parent, 0o755)
    os.chmod(env.data_dir, 0o755)
    assert S.run(env) == 0
    assert stat.S_IMODE(env.config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(env.data_dir.stat().st_mode) == 0o700


def test_next_command_names_home(tmp_path):
    env = build(tmp_path, ["", "", "n", ""], secrets=[KEY], next_cmd="ZEROOUT_HOME=/h zeroout run")
    assert S.run(env) == 0 and "  next      ZEROOUT_HOME=/h zeroout run" in text(env)


def test_main_with_home_prints_home_in_next_line(tmp_path, monkeypatch, capsys):
    import getpass
    import shlex
    import urllib.request

    home = tmp_path / "my home"  # a space, so the quoting is exercised
    monkeypatch.setenv("ZEROOUT_HOME", "")  # restored afterwards; main sets it for this run
    monkeypatch.delenv("ZEROOUT_TF_KEY", raising=False)
    answers = iter(["", "", "n", ""])  # base URL, model 1, no Twilio, cap
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": KEY)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a tty, and no env key, so getpass is used
    monkeypatch.setattr(urllib.request, "urlopen", Http(models_response(IDS)))
    assert S.main(["--home", str(home), "--skip-model"]) == 0
    assert f"  next      ZEROOUT_HOME={shlex.quote(str(home))} zeroout run" in capsys.readouterr().out
    assert dotenv_values(home / "zeroout.env", interpolate=False)["TF_API_KEY"] == KEY
