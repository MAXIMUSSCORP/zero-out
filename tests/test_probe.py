from types import SimpleNamespace

import pytest

from zeroout.config import Settings
from zeroout.models import Step
from zeroout.probe import CallFailed, ProbeResult, twilio_probe, twiml_for


def test_twiml_root_is_pause_and_hangup():
    assert twiml_for([]) == '<Response><Pause length="40"/><Hangup/></Response>'


def test_twiml_13_5_seconds():
    assert twiml_for([Step("2", 13.5)]) == (
        '<Response><Pause length="13"/><Play digits="w2"/><Pause length="40"/><Hangup/></Response>'
    )


def test_twiml_7_7_rounds_remainder_up():
    assert '<Pause length="7"/><Play digits="ww1"/>' in twiml_for([Step("1", 7.7)])


def test_twiml_zero_pause_skipped_and_whole_seconds():
    x = twiml_for([Step("1", 0.5), Step("#", 3.0)])
    assert x.startswith('<Response><Play digits="w1"/><Pause length="3"/><Play digits="#"/>')
    assert 'length="0"' not in x


def st():
    return Settings("", "", "", "", "", "SID", "TOK", "+15550009999", "", 1.0, "")


class FakeClient:
    def __init__(self, statuses, recs=(), duration="42"):
        self.statuses = list(statuses)
        self.recs = list(recs)
        self.duration = duration
        self.created = None
        self.updated = []
        self.rec_calls = 0
        outer = self

        class Call:
            def fetch(self_):
                s = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                return SimpleNamespace(status=s, duration=outer.duration)

            def update(self_, **kw):
                outer.updated.append(kw)

        class Calls:
            def create(self_, **kw):
                outer.created = kw
                return SimpleNamespace(sid="CA1")

            def __call__(self_, sid):
                assert sid == "CA1"
                return Call()

        class Recs:
            def list(self_, **kw):
                outer.rec_calls += 1
                outer.rec_kw = kw
                return outer.recs if outer.rec_calls > 1 or not outer.recs else []

        self.calls = Calls()
        self.recordings = Recs()


def test_completed_call_downloads_wav():
    c = FakeClient(["queued", "in-progress", "completed"], [SimpleNamespace(sid="RE9")])
    sleeps, urls = [], []

    def dl(url, sid, token):
        urls.append((url, sid, token))
        return b"WAVDATA"

    p = twilio_probe(st(), client=c, sleep=sleeps.append, download=dl)
    r = p("+18005550000", [Step("1", 5.0)])
    assert r == ProbeResult(b"WAVDATA", 42.0)
    assert urls == [("https://api.twilio.com/2010-04-01/Accounts/SID/Recordings/RE9.wav", "SID", "TOK")]
    assert c.created["to"] == "+18005550000" and c.created["from_"] == "+15550009999"
    assert c.created["record"] is True and 'digits="1"' in c.created["twiml"]
    assert c.rec_kw == {"call_sid": "CA1", "limit": 1}
    assert sleeps[:2] == [3, 3]


@pytest.mark.parametrize("status", ["busy", "failed", "no-answer", "canceled"])
def test_bad_status_raises(status):
    c = FakeClient(["ringing", status])
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+18005550000", [])


def test_timeout_hangs_up_then_raises():
    c = FakeClient(["in-progress"])
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+18005550000", [])
    assert c.updated == [{"status": "completed"}]


def test_missing_recording_raises():
    c = FakeClient(["completed"], [])
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+18005550000", [])


# --- Twilio error handling, billing, guards ---

from twilio.base.exceptions import TwilioRestException

from zeroout.paths import effective_wait


def test_twiml_uses_effective_wait():
    assert twiml_for([Step("1", 7.01)]) == (
        '<Response><Pause length="7"/><Play digits="w1"/><Pause length="40"/><Hangup/></Response>'
    )


def test_non_toll_free_raises_value_error_before_calling():
    c = FakeClient(["completed"])
    with pytest.raises(ValueError):
        twilio_probe(st(), client=c, sleep=lambda s: None)("+12125550000", [])
    assert c.created is None


def test_call_time_limit():
    c = FakeClient(["completed"], [SimpleNamespace(sid="RE9")])
    twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"W")(
        "+18005550000", [Step("1", 5.0)])
    assert c.created["time_limit"] == int(5.0 + 40 + 30)


def boom_exc(kind):
    return TwilioRestException(500, "http://x", "down") if kind == "twilio" else OSError("net")


@pytest.mark.parametrize("kind", ["twilio", "os"])
@pytest.mark.parametrize("where", ["create", "fetch", "list"])
def test_twilio_errors_become_call_failed(kind, where):
    c = FakeClient(["completed"], [SimpleNamespace(sid="RE9")])
    exc = boom_exc(kind)

    def raiser(*a, **k):
        raise exc

    if where == "create":
        c.calls.create = raiser
    elif where == "fetch":
        c.calls.__class__.__call__ = lambda s, sid: SimpleNamespace(fetch=raiser)
    else:
        c.recordings.list = raiser
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"W")(
            "+18005550000", [])


def test_call_failed_seconds_default_zero():
    assert CallFailed("x").seconds == 0
    assert CallFailed("x", seconds=5).seconds == 5


@pytest.mark.parametrize("status", ["busy", "failed", "no-answer"])
def test_bad_status_bills_nothing(status):
    c = FakeClient(["ringing", status])
    with pytest.raises(CallFailed) as e:
        twilio_probe(st(), client=c, sleep=lambda s: None)("+18005550000", [])
    assert e.value.seconds == 0


def test_timeout_bills_elapsed_time():
    c = FakeClient(["in-progress"])
    sleeps = []
    with pytest.raises(CallFailed) as e:
        twilio_probe(st(), client=c, sleep=sleeps.append)("+18005550000", [])
    assert e.value.seconds == sum(sleeps) > 0


def test_missing_recording_bills_call_duration():
    c = FakeClient(["completed"], [], duration="42")
    with pytest.raises(CallFailed) as e:
        twilio_probe(st(), client=c, sleep=lambda s: None)("+18005550000", [])
    assert e.value.seconds == 42.0


def test_recording_deleted_after_download():
    c = FakeClient(["completed"], [SimpleNamespace(sid="RE9")])
    deleted = []
    c.recordings.__class__.__call__ = lambda s, sid: SimpleNamespace(delete=lambda: deleted.append(sid))
    twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"W")(
        "+18005550000", [])
    assert deleted == ["RE9"]


def test_recording_delete_failure_is_ignored():
    c = FakeClient(["completed"], [SimpleNamespace(sid="RE9")])

    def bad(s, sid):
        raise TwilioRestException(500, "http://x", "no")

    c.recordings.__class__.__call__ = bad
    r = twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"W")(
        "+18005550000", [])
    assert r.wav == b"W"


# --- _download redirect handling ---

import http.server
import threading

from zeroout.probe import _download


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_download_does_not_forward_auth_on_redirect():
    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"AUDIO")

        def log_message(self, *a):
            pass

    target = _serve(Target)
    first_auth = []

    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            first_auth.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/f.wav")
            self.end_headers()

        def log_message(self, *a):
            pass

    origin = _serve(Origin)
    try:
        assert _download(f"http://127.0.0.1:{origin.server_port}/r.wav", "SID", "TOK") == b"AUDIO"
    finally:
        origin.shutdown()
        target.shutdown()
    assert first_auth[0].startswith("Basic ")
    assert seen == [None]


def test_download_follows_at_most_one_redirect():
    class Loop(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/again")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _serve(Loop)
    try:
        with pytest.raises(OSError):
            _download(f"http://127.0.0.1:{srv.server_port}/r.wav", "SID", "TOK")
    finally:
        srv.shutdown()
