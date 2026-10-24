import os
from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import FakeMenu
from zeroout import db, tree
from zeroout.budget import Budget, can_spend
from zeroout.config import Settings
from zeroout.crawl import crawl
from zeroout.models import Observation, Option
from zeroout.probe import CallFailed, ProbeResult

YES = lambda a, b: True


def settings(tmp_path, cap=10.0):
    return Settings("", "", "", "", "", "", "", "", "", cap, "", audio_dir=str(tmp_path / "audio"))


def menu():
    return Observation("1 billing 2 agent", [Option("1", "billing"), Option("2", "agent")], "menu", 4.0)


def fake_tree():
    return {
        "": menu(),
        "1": Observation("please hold", [], "hold", 2.0),
        "2": Observation("a person", [], "human", 1.0),
    }


def setup(tmp_path, cap=10.0):
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550000")
    return conn, cid, settings(tmp_path, cap)


def status(conn, cid, path):
    return db.get_node(conn, cid, path)["status"]


def run_row(conn, rid):
    return conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()


def n_obs(conn):
    return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def test_crawl_maps_whole_fake_menu(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert [status(conn, cid, p) for p in ("", "1", "2")] == ["mapped", "mapped", "human"]
    assert db.get_node(conn, cid, "1")["outcome"] == "hold"
    row = run_row(conn, rid)
    assert row["calls"] == 3 and row["ended_at"] and row["kind"] == "crawl"
    assert row["minutes"] > 0 and row["cost_usd"] == pytest.approx(row["minutes"] * st.rate_per_min)
    assert fm.calls == ["", "1", "2"]
    files = os.listdir(tmp_path / "audio" / str(cid))
    assert f"{rid}-root-1.wav" in files and f"{rid}-1-1.wav" in files


def test_budget_stops_after_one_call(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_calls=1), sleep=lambda s: None)
    assert status(conn, cid, "") == "mapped"
    assert status(conn, cid, "1") == status(conn, cid, "2") == "unexplored"
    assert run_row(conn, rid)["calls"] == 1


def test_max_minutes_stops(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_minutes=0.01), sleep=lambda s: None)
    assert run_row(conn, rid)["calls"] == 1


def test_spend_cap_blocks_calls(tmp_path):
    conn, cid, st = setup(tmp_path, cap=0)
    fm = FakeMenu(fake_tree())
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert fm.calls == [] and run_row(conn, rid)["calls"] == 0
    assert run_row(conn, rid)["ended_at"]


def test_spend_cap_checked_before_retries(tmp_path):
    conn, cid, st = setup(tmp_path, cap=0.0001)
    n = []

    def probe(number, steps):
        n.append(1)
        return ProbeResult(b"", 60.0)

    crawl(conn, st, cid, probe, lambda w: None, YES, sleep=lambda s: None)
    assert len(n) == 1


def test_empty_audio_is_retried_not_merged(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})
    results = [ProbeResult(b"", 5.0), ProbeResult(b"", 5.0)]
    sleeps = []

    def probe(number, steps):
        return results.pop(0) if results else fm.probe(number, steps)

    seen = []

    def listen(w):
        seen.append(w)
        return fm.listen(w)

    rid = crawl(conn, st, cid, probe, listen, YES, sleep=sleeps.append)
    assert status(conn, cid, "") == "mapped"
    assert n_obs(conn) == 1 and len(seen) == 1
    assert sleeps == [2, 4]
    assert run_row(conn, rid)["calls"] == 3


def test_zero_second_tail_counts_as_failure(tmp_path):
    from zeroout.segment import tail

    conn, cid, st = setup(tmp_path)
    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})
    calls = []

    def probe(number, steps):
        calls.append(1)
        good = fm.probe(number, steps)
        return ProbeResult(tail(good.wav, 10), 1.0) if len(calls) == 1 else good

    crawl(conn, st, cid, probe, fm.listen, YES, sleep=lambda s: None)
    assert status(conn, cid, "") == "mapped" and len(calls) == 2 and n_obs(conn) == 1


def test_callfailed_retry_sleeps_then_succeeds(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})
    fails = [CallFailed("busy"), CallFailed("busy")]
    sleeps = []

    def probe(number, steps):
        if fails:
            raise fails.pop()
        return fm.probe(number, steps)

    rid = crawl(conn, st, cid, probe, fm.listen, YES, sleep=sleeps.append)
    assert sleeps == [2, 4]
    assert status(conn, cid, "") == "mapped"
    assert run_row(conn, rid)["calls"] == 3


def test_path_failing_three_times_is_skipped_and_crawl_continues(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    attempts = []

    def probe(number, steps):
        path = ".".join(s.key for s in steps)
        attempts.append(path)
        if path == "1":
            raise CallFailed("failed")
        return fm.probe(number, steps)

    sleeps = []
    rid = crawl(conn, st, cid, probe, fm.listen, YES, sleep=sleeps.append)
    assert attempts.count("1") == 3
    assert status(conn, cid, "1") == "unexplored" and status(conn, cid, "2") == "human"
    assert sleeps == [2, 4]
    assert run_row(conn, rid)["calls"] == 5


def test_listen_none_does_not_merge_or_loop(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    rid = crawl(conn, st, cid, fm.probe, lambda w: None, YES, sleep=lambda s: None)
    assert n_obs(conn) == 0 and status(conn, cid, "") == "unexplored"
    assert fm.calls == [""]
    assert run_row(conn, rid)["calls"] == 1


def test_listen_exception_is_unparsed(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())

    def boom(w):
        raise RuntimeError("api down")

    crawl(conn, st, cid, fm.probe, boom, YES, sleep=lambda s: None)
    assert n_obs(conn) == 0 and fm.calls == [""]


def test_invalid_input_reprobes_once_with_longer_wait(tmp_path):
    conn, cid, st = setup(tmp_path)
    t = fake_tree()
    fm = FakeMenu(t)
    waits = []
    first = {"1": True}

    def probe(number, steps):
        if steps and steps[-1].key == "1":
            waits.append(steps[-1].wait)
        return fm.probe(number, steps)

    def listen(w):
        obs = fm.listen(w)
        if obs is t["1"] and first.pop("1", None):
            return Observation("invalid entry", [], "invalid_input", 1.0)
        return obs

    rid = crawl(conn, st, cid, probe, listen, YES, sleep=lambda s: None)
    assert waits == [5.5, 7.5]
    assert status(conn, cid, "1") == "mapped"
    assert n_obs(conn) == 3
    assert run_row(conn, rid)["calls"] == 4


def test_invalid_input_twice_gives_up(tmp_path):
    conn, cid, st = setup(tmp_path)
    t = fake_tree()
    fm = FakeMenu(t)

    def listen(w):
        obs = fm.listen(w)
        return Observation("bad", [], "invalid_input", 1.0) if obs is t["1"] else obs

    rid = crawl(conn, st, cid, fm.probe, listen, YES, sleep=lambda s: None)
    assert fm.calls.count("1") == 2
    assert status(conn, cid, "1") == "unexplored"
    assert status(conn, cid, "2") == "human"


def test_max_depth_respected(tmp_path):
    conn, cid, st = setup(tmp_path)
    deep = {
        "": menu(),
        "1": Observation("sub", [Option("1", "x")], "menu", 1.0),
        "1.1": Observation("sub", [Option("1", "x")], "menu", 1.0),
        "2": Observation("a person", [], "human", 1.0),
    }
    fm = FakeMenu(deep)
    crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_depth=1), sleep=lambda s: None)
    assert status(conn, cid, "1") == "mapped"
    assert status(conn, cid, "1.1") == "unexplored"
    assert "1.1" not in fm.calls


def test_can_spend_ignores_yesterday(tmp_path):
    conn, cid, st = setup(tmp_path, cap=1.0)
    y = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO runs (company_id, kind, calls, minutes, cost_usd, started_at) "
        "VALUES (?, 'crawl', 1, 1, 5.0, ?)", (cid, y))
    conn.commit()
    assert can_spend(conn, st)
    rid = db.start_run(conn, cid, "crawl", "t")
    conn.execute("UPDATE runs SET cost_usd = 1.0 WHERE id = ?", (rid,))
    conn.commit()
    assert not can_spend(conn, st)


def test_recording_segments_decode_per_boundary():
    from zeroout.segment import segment
    from tests.fakes import decode

    rec = FakeMenu.recording("2.1", [3.0, 6.0])
    assert [decode(s) for s in segment(rec, [3.0, 6.0])] == ["", "2", "2.1"]
    assert decode(b"junk") is None


def test_undecided_counts_as_done_for_path(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    seen = []

    def same(a, b):
        seen.append(1)
        return None

    crawl(conn, st, cid, fm.probe, fm.listen, same, sleep=lambda s: None)
    # re-crawl a mapped tree: nothing is unexplored, so same is never needed
    conn.execute("UPDATE nodes SET status='stale' WHERE path='1'")
    conn.commit()
    crawl(conn, st, cid, fm.probe, fm.listen, same, sleep=lambda s: None)
    assert seen == [1]
    assert fm.calls.count("1") == 2
    assert status(conn, cid, "1") == "stale"


def test_budget_is_per_company_per_day(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())
    first = crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_calls=3), sleep=lambda s: None)
    assert run_row(conn, first)["calls"] == 3
    conn.execute("UPDATE nodes SET status='stale'")
    conn.commit()
    second = crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_calls=3), sleep=lambda s: None)
    assert run_row(conn, second)["calls"] == 0
    assert len(fm.calls) == 3


def test_run_row_records_only_own_calls_and_minutes(tmp_path):
    conn, cid, st = setup(tmp_path)
    prev = db.start_run(conn, cid, "crawl", "x")
    conn.execute("UPDATE runs SET calls = 5, minutes = 5 WHERE id = ?", (prev,))
    conn.commit()
    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert run_row(conn, rid)["calls"] == 1
    assert run_row(conn, rid)["minutes"] == 1


def test_minutes_billed_per_started_minute(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})

    def probe(number, steps):
        r = fm.probe(number, steps)
        return ProbeResult(r.wav, 61.0)

    rid = crawl(conn, st, cid, probe, fm.listen, YES, sleep=lambda s: None)
    assert run_row(conn, rid)["minutes"] == 2


def test_failed_call_billed_by_its_seconds(tmp_path):
    conn, cid, st = setup(tmp_path)

    def probe(number, steps):
        raise CallFailed("timed out", seconds=90)

    rid = crawl(conn, st, cid, probe, lambda w: None, YES, sleep=lambda s: None)
    assert run_row(conn, rid)["calls"] == 3 and run_row(conn, rid)["minutes"] == 6


def test_failed_call_with_no_seconds_bills_nothing(tmp_path):
    conn, cid, st = setup(tmp_path)

    def probe(number, steps):
        raise CallFailed("busy")

    rid = crawl(conn, st, cid, probe, lambda w: None, YES, sleep=lambda s: None)
    assert run_row(conn, rid)["minutes"] == 0


def test_merge_exception_is_logged_and_crawl_continues(tmp_path):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu(fake_tree())

    def same(a, b):
        raise RuntimeError("model down")

    crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    conn.execute("UPDATE nodes SET status='stale' WHERE path IN ('', '1')")
    conn.commit()
    fm.calls.clear()
    rid = crawl(conn, st, cid, fm.probe, fm.listen, same, sleep=lambda s: None)
    assert sorted(fm.calls) == ["", "1"]
    assert run_row(conn, rid)["ended_at"]


def test_tail_cut_at_effective_press_time(tmp_path, monkeypatch):
    import zeroout.crawl as crawl_mod

    conn, cid, st = setup(tmp_path)
    cuts = []
    real = crawl_mod.tail
    monkeypatch.setattr(crawl_mod, "tail", lambda wav, s: (cuts.append(s), real(wav, s))[1])
    fm = FakeMenu({"": menu(), "1": Observation("hold", [], "hold", 2.0)})
    crawl(conn, st, cid, fm.probe, fm.listen, YES, Budget(max_depth=1), sleep=lambda s: None)
    # root prompt 4.0 + margin 1.5 = 5.5
    assert cuts[0] == 0 and 5.5 in cuts


def test_tail_cut_uses_rounded_up_wait(tmp_path, monkeypatch):
    import zeroout.crawl as crawl_mod

    conn, cid, st = setup(tmp_path)
    conn.execute("INSERT INTO nodes (company_id, path, prompt_seconds, status) VALUES (?, '', 4.2, 'mapped')", (cid,))
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '1', 'unexplored')", (cid,))
    conn.commit()
    cuts = []
    real = crawl_mod.tail
    monkeypatch.setattr(crawl_mod, "tail", lambda wav, s: (cuts.append(s), real(wav, s))[1])
    fm = FakeMenu({"1": Observation("hold", [], "hold", 2.0)})
    crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert cuts == [6.0]  # 4.2 + 1.5 = 5.7 -> 6.0


@pytest.mark.parametrize("outcome", ["human", "voicemail"])
def test_no_audio_kept_for_real_people(tmp_path, outcome):
    conn, cid, st = setup(tmp_path)
    fm = FakeMenu({"": Observation("hello", [], outcome, 1.0)})
    rid = crawl(conn, st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert not os.path.exists(tmp_path / "audio" / str(cid)) or os.listdir(tmp_path / "audio" / str(cid)) == []
    assert conn.execute("SELECT audio_url FROM observations").fetchone()[0] == ""


def test_crawl_run_updates_hold_lock(tmp_path):
    import threading

    conn, cid, st = setup(tmp_path)
    seen = []

    def grab():
        ok = db.LOCK.acquire(timeout=0.05)
        seen.append(ok)
        if ok:
            db.LOCK.release()

    class Spy:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            if sql.startswith("UPDATE runs"):
                t = threading.Thread(target=grab)
                t.start()
                t.join()
            return self.c.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self.c, n)

    fm = FakeMenu({"": Observation("hold on", [], "hold", 1.0)})
    crawl(Spy(conn), st, cid, fm.probe, fm.listen, YES, sleep=lambda s: None)
    assert seen and not any(seen)
