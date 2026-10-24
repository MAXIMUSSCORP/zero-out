import os
import sqlite3

import pytest

from tests.fakes import FakeMenu
from zeroout import db
from zeroout.config import Settings
from zeroout.connect import _run_connect, ingest_connect_recording, start_connect
from zeroout.crawl import crawl
from zeroout.models import Observation, Option
from zeroout.paths import build_path, press_times
from zeroout.probe import ProbeResult

YES = lambda a, b: True
USER = "+14155550123"


def settings(tmp_path):
    return Settings("", "", "", "", "", "SID", "TOK", "+15550001111", "", 10.0, "", audio_dir=str(tmp_path / "audio"))


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
    def __init__(self, events, user_statuses, company_statuses=("queued", "in-progress")):
        self.events, self.clock = events, [0.0]
        self.status = {"CO": list(company_statuses), "US": list(user_statuses)}
        self.created: list[dict] = []

    def create(self, **kw):
        self.created.append(kw)
        self.events.append(("dial", kw["to"], self.clock[0]))
        return Obj(sid="US")

    @property
    def calls(self):
        client = self

        class Calls:
            create = staticmethod(client.create)

            def __call__(self, sid):
                class Call:
                    def fetch(_):
                        q = client.status[sid]
                        return Obj(status=q.pop(0) if len(q) > 1 else q[0])

                    def update(_, **kw):
                        client.events.append(("update", sid, kw["status"]))

                    @property
                    def recordings(_):
                        class Recs:
                            def create(_):
                                client.events.append(("rec_start", sid, client.clock[0]))
                                return Obj(sid="RE")

                            def __call__(_, rsid):
                                class Rec:
                                    def update(_, **kw):
                                        client.events.append(("rec_stop", sid, kw["status"]))
                                return Rec()
                        return Recs()
                return Call()
        return Calls()


def run_it(tmp_path, user_statuses):
    conn, cid, s, menu = mapped(tmp_path)
    steps, times, wav = recording(conn, cid, s)
    run = db.start_run(conn, cid, "connect", "connect")
    events: list = []
    client = FakeClient(events, user_statuses)

    def sleep(x):
        client.clock[0] += x

    def download(url, sid, tok):
        events.append(("download", url))
        return wav

    done = []
    _run_connect(conn, s, run, cid, "1", USER, steps, client, "CO", sleep, lambda: client.clock[0],
                 menu.listen, YES, download, lambda: done.append(1))
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
