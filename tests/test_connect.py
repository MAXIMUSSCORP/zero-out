import os
import sqlite3

import pytest

from tests.fakes import FakeMenu
from zeroout import db
from zeroout.config import Settings
from zeroout import connect as connect_mod
from zeroout.connect import RESERVE_MINUTES, _run_connect, ingest_connect_recording, start_connect
from zeroout.crawl import crawl
from zeroout.listen import NO_MODEL_MSG
from zeroout.models import Observation, Option
from zeroout.paths import build_path, press_times
from zeroout.probe import ProbeResult

YES = lambda a, b: True
USER = "+14155550123"


def settings(tmp_path):
    return Settings("", "", "", "", "SID", "TOK", "+15550001111", "", 10.0, "", audio_dir=str(tmp_path / "audio"))


def fake_tree():
    return {
        "": Observation("1 billing 2 agent", [Option("1", "billing"), Option("2", "agent")], "menu", 4.0),
        "1": Observation("please hold", [], "hold", 2.0),
        "2": Observation("a person", [], "human", 1.0),
    }


def mapped(tmp_path):
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550000")
    s = settings(tmp_path)
    menu = FakeMenu(fake_tree())
    crawl(conn, s, cid, menu.probe, menu.listen, YES, sleep=lambda x: None)
    return conn, cid, s, menu


def conf(conn, cid, path):
    return db.get_node(conn, cid, path)["confirmations"]


def recording(conn, cid, s, path="1"):
    steps = build_path(conn, cid, path, s.key_margin)
    times = press_times(steps)
    return steps, times, FakeMenu.recording(path, times)


def test_connect_recording_confirms_every_node_on_path(tmp_path):
    conn, cid, s, menu = mapped(tmp_path)
    _, times, wav = recording(conn, cid, s)
    run = db.start_run(conn, cid, "connect", "connect")
    assert ingest_connect_recording(conn, s, cid, "1", wav, times, run, menu.listen, YES) == ["confirmed", "confirmed"]
    assert conf(conn, cid, "") == 2 and conf(conn, cid, "1") == 2
    assert os.path.exists(os.path.join(s.audio_dir, str(cid), f"{run}-connect-root.wav"))
    assert os.path.exists(os.path.join(s.audio_dir, str(cid), f"{run}-connect-1.wav"))


def test_unparsed_and_empty_segments_do_not_merge(tmp_path):
    conn, cid, s, menu = mapped(tmp_path)
    _, times, wav = recording(conn, cid, s)
    run = db.start_run(conn, cid, "connect", "connect")
    boom = lambda w: 1 / 0
    assert ingest_connect_recording(conn, s, cid, "1", wav, times, run, boom, YES) == ["unparsed", "unparsed"]
    assert ingest_connect_recording(conn, s, cid, "1", b"", times, run, menu.listen, YES) == ["unparsed", "unparsed"]
    none = lambda w: None
    assert ingest_connect_recording(conn, s, cid, "1", wav, times, run, none, YES) == ["unparsed", "unparsed"]
    assert conf(conn, cid, "") == 1 and conf(conn, cid, "1") == 1


def test_extra_segments_ignored(tmp_path):
    conn, cid, s, menu = mapped(tmp_path)
    steps, times, _ = recording(conn, cid, s)
    wav = FakeMenu.recording("1", [*times, times[-1] + 5, times[-1] + 9])
    run = db.start_run(conn, cid, "connect", "connect")
    assert ingest_connect_recording(conn, s, cid, "1", wav, [*times, times[-1] + 5, times[-1] + 9], run, menu.listen, YES) == ["confirmed", "confirmed"]


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, events, user_statuses, company_statuses=("queued", "in-progress"),
                 stop_failures=0, stop_always_fails=False, user_fetch_raises=False,
                 user_create_raises=False, rec_delay=0.0):
        self.events, self.clock = events, [0.0]
        self.status = {"CO": list(company_statuses), "US": list(user_statuses)}
        self.created: list[dict] = []
        self.rec_kwargs: list[dict] = []
        self.stop_failures, self.stop_always_fails = stop_failures, stop_always_fails
        self.user_fetch_raises, self.user_create_raises = user_fetch_raises, user_create_raises
        self.rec_delay = rec_delay

    def create(self, **kw):
        self.created.append(kw)
        if self.user_create_raises:
            raise RuntimeError("create boom")
        self.events.append(("dial", kw["to"], self.clock[0]))
        return Obj(sid="US")

    @property
    def recordings(self):
        client = self

        def rec(rsid):
            class Rec:
                def delete(_):
                    client.events.append(("rec_delete", rsid))
            return Rec()
        return rec

    @property
    def calls(self):
        client = self

        class Calls:
            create = staticmethod(client.create)

            def __call__(self, sid):
                class Call:
                    def fetch(_):
                        if sid == "US" and client.user_fetch_raises:
                            raise RuntimeError("poll boom")
                        q = client.status[sid]
                        return Obj(status=q.pop(0) if len(q) > 1 else q[0])

                    def update(_, **kw):
                        client.events.append(("update", sid, kw["status"]))

                    @property
                    def recordings(_):
                        class Recs:
                            def create(_, **kw):
                                client.rec_kwargs.append(kw)
                                client.clock[0] += client.rec_delay
                                client.events.append(("rec_start", sid, client.clock[0]))
                                return Obj(sid="RE")

                            def __call__(_, rsid):
                                class Rec:
                                    def update(_, **kw):
                                        if client.stop_always_fails or client.stop_failures > 0:
                                            client.stop_failures -= 1
                                            client.events.append(("rec_stop_failed", sid))
                                            raise RuntimeError("stop boom")
                                        client.events.append(("rec_stop", sid, kw["status"]))
                                return Rec()
                        return Recs()
                return Call()
        return Calls()


def run_it(tmp_path, user_statuses, **client_kw):
    conn, cid, s, menu = mapped(tmp_path)
    steps, times, wav = recording(conn, cid, s)
    run = db.start_run(conn, cid, "connect", "connect")
    events: list = []
    client = FakeClient(events, user_statuses, **client_kw)

    def sleep(x):
        client.clock[0] += x

    def download(url, sid, tok):
        events.append(("download", url))
        return wav

    done = []
    _run_connect(conn, s, run, cid, "1", USER, steps, client, "CO", sleep, lambda: client.clock[0],
                 menu.listen, YES, download, lambda: done.append(1))
    run_it.client = client
    return conn, cid, run, events, times, done


def test_order_recording_stopped_before_anything_once_user_in_progress(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["queued", "ringing", "in-progress"])
    kinds = [e[0] if e[0] != "dial" else f"dial:{e[1]}" for e in events]
    assert kinds[0] == "rec_start"
    dial = next(e for e in events if e[0] == "dial")
    assert dial[2] >= times[-1] + 2
    i = kinds.index("rec_stop")
    assert kinds[i - 1] == f"dial:{USER}" or "dial:" + USER in kinds[:i]
    assert kinds[i + 1] == "download"
    assert not any(e == ("update", "CO", "completed") for e in events)
    assert done == [1]
    assert conf(conn, cid, "1") == 2
    r = conn.execute("SELECT calls, ended_at FROM runs WHERE id = ?", (run,)).fetchone()
    assert r["calls"] == 2 and r["ended_at"]


def test_user_no_answer_stops_recording_and_hangs_up_company(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["ringing", "no-answer"])
    assert ("rec_stop", "CO", "stopped") in events
    assert ("update", "CO", "completed") in events
    assert done == [1]


def test_user_phone_not_in_db(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"])
    for t in ("runs", "observations", "nodes", "companies"):
        for row in conn.execute(f"SELECT * FROM {t}"):
            assert USER not in str(tuple(row))


def test_start_connect_validates_before_dialing(tmp_path):
    conn, cid, s, menu = mapped(tmp_path)
    client = FakeClient([], ["in-progress"])
    other = db.add_company(conn, "Cell", "+14155550100")
    with pytest.raises(ValueError):
        start_connect(conn, s, other, "", USER, client=client)
    with pytest.raises(ValueError):
        start_connect(conn, s, cid, "9", USER, client=client)
    conn.execute("UPDATE nodes SET status = 'unexplored' WHERE company_id = ? AND path = '1'", (cid,))
    with pytest.raises(ValueError):
        start_connect(conn, s, cid, "1", USER, client=client)
    assert client.created == []


def hung_up(events):
    return ("update", "CO", "completed") in events, ("update", "US", "completed") in events


def run_row(conn, run):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()


def test_exception_polling_user_call_stops_recording_and_hangs_up_both(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["queued"], user_fetch_raises=True)
    assert ("rec_stop", "CO", "stopped") in events
    assert hung_up(events) == (True, True)
    assert done == [1]
    assert run_row(conn, run)["ended_at"]


def test_exception_from_user_create_stops_recording_and_hangs_up_company(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], user_create_raises=True)
    assert ("rec_stop", "CO", "stopped") in events
    assert ("update", "CO", "completed") in events
    assert ("update", "US", "completed") not in events  # no user leg exists
    assert done == [1]
    assert run_row(conn, run)["ended_at"]


def test_first_stop_attempt_raising_is_retried(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], stop_failures=1)
    assert events.count(("rec_stop_failed", "CO")) == 1
    assert ("rec_stop", "CO", "stopped") in events
    assert hung_up(events) == (False, False)  # retry succeeded: clean exit, call continues
    assert conf(conn, cid, "1") == 2


def test_stop_failing_three_times_per_pass_hangs_up_both(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], stop_always_fails=True)
    assert hung_up(events) == (True, True)
    assert ("rec_stop", "CO", "stopped") not in events
    assert done == [1]


def test_user_never_joining_hangs_up_both(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["ringing", "no-answer"])
    assert hung_up(events) == (True, True)


def test_recording_is_company_side_only(tmp_path):
    run_it(tmp_path, ["in-progress"])
    assert run_it.client.rec_kwargs == [{"recording_track": "inbound"}]


def test_run_accounting_written_with_reserve(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"])
    r = run_row(conn, run)
    elapsed_min = run_it.client.clock[0] / 60
    assert r["calls"] == 2 and r["ended_at"]
    assert RESERVE_MINUTES < r["minutes"] <= RESERVE_MINUTES + elapsed_min + 1
    assert r["minutes"] == int(r["minutes"])
    assert r["cost_usd"] == pytest.approx(r["minutes"] * 0.014)


def test_failed_run_still_counted(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], user_create_raises=True)
    r = run_row(conn, run)
    assert r["minutes"] >= RESERVE_MINUTES and r["cost_usd"] > 0 and r["calls"] == 2


def test_company_never_connecting_is_counted_and_hung_up(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], company_statuses=("busy",))
    assert ("update", "CO", "completed") in events
    assert run_row(conn, run)["minutes"] >= RESERVE_MINUTES


def test_both_calls_have_time_limit(tmp_path, monkeypatch):
    run_it(tmp_path, ["in-progress"])
    assert run_it.client.created[0]["time_limit"] == 1800
    conn, cid, s, menu = mapped(tmp_path / "b")
    client = FakeClient([], ["in-progress"])
    monkeypatch.setattr(connect_mod.threading, "Thread", FakeThread)
    start_connect(conn, s, cid, "1", USER, client=client, listen=menu.listen, same_meaning=YES)
    assert client.created[0]["time_limit"] == 1800


def test_segments_offset_by_recording_start_delay(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(connect_mod, "ingest_connect_recording",
                        lambda conn, s, cid, path, wav, times, *a: seen.setdefault("times", times))
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"], rec_delay=1.0)
    assert seen["times"] == [max(t - 1.0, 0) for t in times]
    assert seen["times"] != times


def test_no_audio_saved_for_human_or_voicemail(tmp_path, monkeypatch):
    conn, cid, s, menu = mapped(tmp_path)
    steps = build_path(conn, cid, "2", s.key_margin)
    times = press_times(steps)
    wav = FakeMenu.recording("2", times)
    run = db.start_run(conn, cid, "connect", "connect")
    merged = []
    real = connect_mod.merge
    monkeypatch.setattr(connect_mod, "merge", lambda *a: merged.append(a[5]) or real(*a))
    ingest_connect_recording(conn, s, cid, "2", wav, times, run, menu.listen, YES)
    assert merged[-1] == ""  # audio_path of the human segment
    assert not os.path.exists(os.path.join(s.audio_dir, str(cid), f"{run}-connect-2.wav"))
    assert os.path.exists(os.path.join(s.audio_dir, str(cid), f"{run}-connect-root.wav"))


def test_twilio_recording_deleted_after_download(tmp_path):
    conn, cid, run, events, times, done = run_it(tmp_path, ["in-progress"])
    kinds = [e[0] for e in events]
    assert ("rec_delete", "RE") in events
    assert kinds.index("rec_delete") > kinds.index("download")


class FakeThread:
    started: list = []

    def __init__(self, target, args, daemon):
        self.target, self.args, self.daemon = target, args, daemon

    def start(self):
        FakeThread.started.append(self)


def test_start_connect_refuses_when_budget_spent(tmp_path, monkeypatch):
    conn, cid, s, menu = mapped(tmp_path)
    monkeypatch.setattr(connect_mod, "can_spend", lambda c, st: False)
    client = FakeClient([], ["in-progress"])
    with pytest.raises(ValueError, match="budget"):
        start_connect(conn, s, cid, "1", USER, client=client)
    assert client.created == []


def test_start_connect_success_starts_thread_and_reserves_cost(tmp_path, monkeypatch):
    conn, cid, s, menu = mapped(tmp_path)
    FakeThread.started = []
    monkeypatch.setattr(connect_mod.threading, "Thread", FakeThread)
    client = FakeClient([], ["in-progress"])
    run = start_connect(conn, s, cid, "1", USER, client=client, listen=menu.listen, same_meaning=YES)
    assert len(FakeThread.started) == 1 and FakeThread.started[0].daemon
    assert FakeThread.started[0].target is _run_connect
    assert client.created[0]["to"] == "+18005550000"
    r = run_row(conn, run)
    assert r["minutes"] == RESERVE_MINUTES
    assert r["cost_usd"] == pytest.approx(RESERVE_MINUTES * s.rate_per_min)
    assert r["ended_at"] is None
    assert USER not in str(FakeThread.started[0].args[:5])


def test_start_connect_failed_dial_releases_reserve(tmp_path, monkeypatch):
    conn, cid, s, menu = mapped(tmp_path)
    FakeThread.started = []
    monkeypatch.setattr(connect_mod.threading, "Thread", FakeThread)
    client = FakeClient([], ["in-progress"], user_create_raises=True)
    with pytest.raises(RuntimeError):
        start_connect(conn, s, cid, "1", USER, client=client, listen=menu.listen, same_meaning=YES)
    r = conn.execute("SELECT minutes, cost_usd, ended_at FROM runs WHERE kind = 'connect'").fetchone()
    assert r["minutes"] == 0 and r["cost_usd"] == 0 and r["ended_at"]
    assert FakeThread.started == []


def test_start_connect_refuses_before_dialing_when_speech_model_missing(tmp_path, monkeypatch):
    from zeroout import hear

    def load(model_dir):
        raise RuntimeError("no model in /srv/secret/folder")

    monkeypatch.setattr(hear, "load", load)
    conn, cid, s, menu = mapped(tmp_path)
    client = FakeClient([], ["in-progress"])
    with pytest.raises(ValueError) as e:
        start_connect(conn, s, cid, "1", USER, client=client, same_meaning=YES)
    assert str(e.value) == NO_MODEL_MSG and "/srv/secret" not in str(e.value)
    assert client.created == []
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'connect'").fetchone()[0] == 0


def test_start_connect_with_real_listener_checks_model_once(tmp_path, monkeypatch):
    from zeroout import hear
    loaded = []
    monkeypatch.setattr(hear, "load", loaded.append)
    conn, cid, s, menu = mapped(tmp_path)
    client = FakeClient([], ["in-progress"])
    start_connect(conn, s, cid, "1", USER, client=client, same_meaning=YES)
    assert loaded == [s.asr_model_dir]
