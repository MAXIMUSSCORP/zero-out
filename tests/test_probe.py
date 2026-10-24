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
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+1", [])


def test_timeout_hangs_up_then_raises():
    c = FakeClient(["in-progress"])
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+1", [])
    assert c.updated == [{"status": "completed"}]


def test_missing_recording_raises():
    c = FakeClient(["completed"], [])
    with pytest.raises(CallFailed):
        twilio_probe(st(), client=c, sleep=lambda s: None, download=lambda *a: b"")("+1", [])
