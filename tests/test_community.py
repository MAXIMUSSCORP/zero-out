import json
import re

import pytest

from datetime import datetime, timezone

from zeroout import community, db, home, phone, tree
from zeroout.community_check import check, observation_uid
from zeroout.models import Observation, Option

YES = lambda a, b: True
NO = lambda a, b: False
UNSURE = lambda a, b: None
NUMBER = "+18885550199"
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")


def setup():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme Wireless", NUMBER)
    tree.ensure_root(conn, cid)
    return conn, cid


def menu(keys=("1", "2"), text="Press a key"):
    return Observation(text, [Option(k, f"opt{k}") for k in keys], "menu", 3.0)


def crawl(conn, cid, path, obs, same=YES, kind="crawl", created=None):
    rid = db.start_run(conn, cid, kind, "t")
    src = "phone" if kind == "phone" else "crawl"
    result = tree.merge(conn, cid, path, obs, rid, None, same, source=src)
    if created:
        conn.execute("UPDATE observations SET created_at=? WHERE id=(SELECT MAX(id) FROM observations)", (created,))
    conn.commit()
    return result


def build(conn, cid):
    return community.build_contribution(conn, cid, now=NOW)


# ---------- file ids ----------

def test_file_ids_are_fresh_random_16_hex():
    ids = {community.new_file_id() for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids)


def test_there_is_no_stable_contributor_id_any_more():
    assert not hasattr(community, "contributor_id")
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    build(conn, cid)
    assert not (home.data_dir() / "contributor_id").exists()


def test_two_files_from_one_machine_share_nothing_in_their_names():
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    a, b = build(conn, cid), build(conn, cid)
    assert a.file_id != b.file_id and a.relpath != b.relpath


# ---------- export ----------

def test_nothing_to_share_is_none():
    conn, cid = setup()
    assert build(conn, cid) is None
    assert community.build_contribution(conn, 999, now=NOW) is None


def test_contribution_has_the_agreed_shape_and_passes_check():
    conn, cid = setup()
    crawl(conn, cid, "", menu(), created="2026-10-25T18:01:02+00:00")
    c = build(conn, cid)
    assert c.relpath == f"trees/8885550199/{TODAY}-{c.file_id}.json"
    assert check(c.relpath, c.body) == []
    assert c.body.endswith(b"\n") and c.body.startswith(b'{\n  "contributed_at"')
    assert c.doc["number"] == "8885550199" and c.doc["name"] == "Acme Wireless"
    assert c.doc["contributed_at"] == TODAY and c.doc["format"] == 1
    assert c.doc["observations"] == [{
        "path": "", "prompt_text": "Press a key", "options": [{"key": "1", "label": "opt1"}, {"key": "2", "label": "opt2"}],
        "outcome": "menu", "prompt_seconds": 3.0, "heard_at": "2026-10-25", "source": "crawl"}]
    assert json.loads(c.body) == c.doc
    assert len(c.observation_ids) == 1
    assert ":" not in json.dumps(c.doc["observations"][0]["heard_at"])  # a date, never a time


def test_only_settled_local_unshared_observations_are_exported():
    conn, cid = setup()
    crawl(conn, cid, "", menu())  # mapped
    crawl(conn, cid, "", menu(), YES)  # confirmed
    crawl(conn, cid, "", menu(), lambda a, b: None)  # undecided: not settled
    crawl(conn, cid, "1", Observation("", [], "hold", 1.0))  # a hold is mapped, only human-like outcomes are terminal
    crawl(conn, cid, "2", Observation("", [], "human", 1.0))  # terminal
    crawl(conn, cid, "", menu(("1", "3")), NO)  # changed
    results = [r[0] for r in conn.execute("SELECT result FROM observations ORDER BY id")]
    assert results == ["mapped", "confirmed", "undecided", "mapped", "terminal", "changed"]
    c = build(conn, cid)
    assert len(c.doc["observations"]) == 5 and len(c.observation_ids) == 5


def test_disputed_and_community_observations_are_not_exported():
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    rid = phone.start_phone_run(conn, cid)
    assert tree.merge(conn, cid, "", menu(("1", "3")), rid, None, NO, source="phone") == "disputed"
    crawl(conn, cid, "1", Observation("", [], "hold", 1.0))
    conn.execute("UPDATE observations SET origin='community' WHERE path='1'")
    conn.commit()
    c = build(conn, cid)
    assert [o["path"] for o in c.doc["observations"]] == [""]


def test_source_is_phone_for_phone_runs():
    conn, cid = setup()
    crawl(conn, cid, "", menu(), kind="phone")
    assert build(conn, cid).doc["observations"][0]["source"] == "phone"


def test_text_and_labels_are_redacted():
    conn, cid = setup()
    obs = Observation("Call 780 555 0199 or mail help@acme.ca", [Option("1", "account 4500 1234")], "menu", 2.0)
    crawl(conn, cid, "", obs)
    o = build(conn, cid).doc["observations"][0]
    assert o["prompt_text"] == "Call [number] or mail [email]"
    assert o["options"] == [{"key": "1", "label": "account [number]"}]


@pytest.mark.parametrize("outcome", ["human", "voicemail"])
def test_human_and_voicemail_text_stays_blank(outcome):
    conn, cid = setup()
    crawl(conn, cid, "", Observation("Hi this is Dana", [Option("1", "Dana")], outcome, 2.0))
    o = build(conn, cid).doc["observations"][0]
    assert o["prompt_text"] == "" and o["options"] == [{"key": "1", "label": ""}]


def test_control_and_invisible_characters_are_stripped_from_text_and_labels():
    conn, cid = setup()
    crawl(conn, cid, "", Observation("Hel​lo‮ there\x07", [Option("1", "bil⁦ling")], "menu", 2.0))
    o = build(conn, cid).doc["observations"][0]
    assert o["prompt_text"] == "Hello there" and o["options"][0]["label"] == "billing"


def test_unshareable_rows_are_left_out_instead_of_raising():
    conn, cid = setup()
    rid = db.start_run(conn, cid, "crawl", "t")

    def row(raw, path=""):
        conn.execute("INSERT INTO observations (company_id, path, run_id, raw_json, result, created_at) VALUES "
                     "(?, ?, ?, ?, 'mapped', '2026-10-25T10:00:00+00:00')", (cid, path, rid, json.dumps(raw)))

    base = {"prompt_text": "ok", "options": [], "outcome": "hold", "prompt_seconds": 1.0}
    row({**base, "prompt_seconds": 500})
    row({**base, "prompt_seconds": -1})
    row({**base, "prompt_seconds": "x"})
    row({**base, "outcome": "robot"})
    row({**base, "options": [{"key": "a", "label": "x"}]})
    row({**base, "options": [{"key": "1", "label": "x"}, {"key": "1", "label": "y"}]})
    row({"prompt_text": "no outcome"})
    conn.execute("INSERT INTO observations (company_id, path, run_id, raw_json, result, created_at) VALUES "
                 "(?, '', ?, 'not json', 'mapped', '2026-10-25T10:00:00+00:00')", (cid, rid))
    assert build(conn, cid) is None
    row(base, "7")
    assert [o["path"] for o in build(conn, cid).doc["observations"]] == ["7"]


def test_heard_at_is_a_utc_date():
    conn, cid = setup()
    crawl(conn, cid, "", menu(), created="2026-10-24T20:01:02-06:00")  # 02:01 UTC the next day
    crawl(conn, cid, "1", Observation("", [], "hold", 1.0), created="2026-10-23 18:05:00")
    heard = [o["heard_at"] for o in build(conn, cid).doc["observations"]]
    assert heard == ["2026-10-23", "2026-10-25"]


def test_observations_are_ordered_oldest_first():
    conn, cid = setup()
    crawl(conn, cid, "", menu(), created="2026-10-25T10:00:00+00:00")
    crawl(conn, cid, "2", Observation("", [], "hold", 1.0), created="2026-10-24T09:00:00+00:00")
    assert [o["path"] for o in build(conn, cid).doc["observations"]] == ["2", ""]


def test_mark_contributed_excludes_them_next_time_and_leaves_new_ones():
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    c = build(conn, cid)
    community.mark_contributed(conn, c.observation_ids, "2026-10-25")
    assert build(conn, cid) is None
    crawl(conn, cid, "1", Observation("", [], "hold", 1.0))
    assert [o["path"] for o in build(conn, cid).doc["observations"]] == ["1"]
    assert conn.execute("SELECT contributed_at FROM observations WHERE path=''").fetchone()[0] == "2026-10-25"


def test_mark_contributed_gives_uids_so_our_own_file_imports_as_already_had():
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    crawl(conn, cid, "1", Observation("", [], "hold", 1.0))
    c = build(conn, cid)
    community.mark_contributed(conn, c.observation_ids, "2026-10-25")
    uids = [r[0] for r in conn.execute("SELECT uid FROM observations ORDER BY id")]
    assert all(uids) and len(set(uids)) == 2
    report = community.import_files(conn, [(c.relpath, c.body)], same_meaning=YES)
    assert (report.already_had, report.observations_added) == (2, 0)


def test_mark_contributed_survives_two_identical_observations():
    conn, cid = setup()
    crawl(conn, cid, "", menu(), created="2026-10-25T18:01:02+00:00")
    crawl(conn, cid, "", menu(), created="2026-10-25T18:01:02+00:00")
    c = build(conn, cid)
    community.mark_contributed(conn, c.observation_ids, "w")
    assert conn.execute("SELECT COUNT(uid) FROM observations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE contributed_at='w'").fetchone()[0] == 2


def test_a_file_holds_at_most_500_and_the_rest_wait():
    conn, cid = setup()
    rid = db.start_run(conn, cid, "crawl", "t")
    for i in range(503):
        conn.execute("INSERT INTO observations (company_id, path, run_id, raw_json, result, created_at) VALUES "
                     "(?, '', ?, ?, 'mapped', ?)", (cid, rid, json.dumps(
                         {"prompt_text": f"t{i}", "options": [], "outcome": "hold", "prompt_seconds": 1.0}),
                         f"2026-10-25T10:{i // 60:02d}:{i % 60:02d}+00:00"))
    conn.commit()
    first = build(conn, cid)
    assert len(first.doc["observations"]) == 500
    community.mark_contributed(conn, first.observation_ids, "w")
    assert len(build(conn, cid).doc["observations"]) == 3


def test_too_deep_paths_are_left_out():
    conn, cid = setup()
    rid = db.start_run(conn, cid, "crawl", "t")
    for path in ("1.2.3.4.5.6", "1.2.3.4.5.6.7"):
        conn.execute("INSERT INTO observations (company_id, path, run_id, raw_json, result, created_at) VALUES "
                     "(?, ?, ?, ?, 'mapped', '2026-10-25T10:00:00+00:00')", (cid, path, rid, json.dumps(
                         {"prompt_text": "", "options": [], "outcome": "hold", "prompt_seconds": 1.0})))
    assert [o["path"] for o in build(conn, cid).doc["observations"]] == ["1.2.3.4.5.6"]


def test_odd_company_names_are_cleaned_and_a_bad_number_is_a_bug():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Ac\nme​  " + "x" * 100, NUMBER)
    crawl(conn, cid, "", menu())
    name = build(conn, cid).doc["name"]
    assert name.startswith("Acme xxx") and len(name) <= 80
    other = db.add_company(conn, "Local", "+14165550100")
    crawl(conn, other, "", menu())
    with pytest.raises(ValueError, match="bug"):
        build(conn, other)


# ---------- import ----------

def obs_doc(path="", text="For billing press 1", keys=("1",), outcome="menu", heard="2026-10-25", **kw):
    d = {"path": path, "prompt_text": text, "options": [{"key": k, "label": f"opt{k}"} for k in keys],
         "outcome": outcome, "prompt_seconds": 5.0, "heard_at": heard, "source": "crawl"}
    d.update(kw)
    return d


def contribution_file(number="8885550199", name="Acme Wireless", observations=None, fid="0123456789abcdef", day="2026-10-27"):
    doc = {"format": 1, "number": number, "name": name, "contributed_at": day,
           "observations": observations or [obs_doc()]}
    return (f"trees/{number}/{day}-{fid}.json", (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())


def fresh(add=True):
    conn = db.connect(":memory:")
    if add:
        db.add_company(conn, "My Acme", NUMBER)
    return conn


def company_id(conn):
    return conn.execute("SELECT id FROM companies WHERE number = ?", (NUMBER,)).fetchone()[0]


def test_invalid_files_are_listed_and_skipped_whole():
    conn = fresh()
    good = contribution_file()
    bad = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(outcome="robot")])
    junk = ("trees/8885550199/2026-10-27-bbbbbbbbbbbbbbbb.json", b"{}")
    report = community.import_files(conn, [bad, junk, good], same_meaning=YES)
    assert report.files == 3 and len(report.invalid) == 2
    assert bad[0] in report.invalid[0] and "outcome" in report.invalid[0]
    assert junk[0] in report.invalid[1]
    assert report.observations_added == 1


def test_one_bad_observation_skips_the_whole_file():
    conn = fresh()
    mixed = contribution_file(observations=[obs_doc(), obs_doc(path="1", outcome="robot")])
    report = community.import_files(conn, [mixed], same_meaning=YES)
    assert report.invalid and report.observations_added == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_a_known_company_is_filled_from_the_files_with_a_community_run():
    conn = fresh()
    report = community.import_files(conn, [contribution_file()], same_meaning=YES)
    assert (report.observations_added, report.already_had, report.offered, report.truncated) == (1, 0, 0, False)
    assert report.results == {"mapped": 1}
    cid = company_id(conn)
    root = db.get_node(conn, cid, "")
    assert (root["status"], root["source"], root["prompt_text"]) == ("mapped", "community", "For billing press 1")
    assert db.get_node(conn, cid, "1")["status"] == "unexplored"
    run = conn.execute("SELECT kind, calls, cost_usd, ended_at FROM runs").fetchone()
    assert (run["kind"], run["calls"], run["cost_usd"]) == ("community", 0, 0) and run["ended_at"]


def test_imported_observations_are_stored_as_community_with_uid_and_no_audio():
    conn = fresh()
    community.import_files(conn, [contribution_file()], same_meaning=YES)
    row = conn.execute("SELECT * FROM observations").fetchone()
    assert (row["origin"], row["audio_url"], row["created_at"]) == ("community", None, "2026-10-25")
    assert row["uid"] == observation_uid("8885550199", obs_doc())
    assert row["contributed_at"] is None


def test_unknown_numbers_become_offers_and_never_companies():
    conn = fresh(add=False)
    a = contribution_file(name="Acme Wireless", fid="aaaaaaaaaaaaaaaa")
    b = contribution_file(name="Acme Wireless", fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(path="1", text="x", keys=())])
    c = contribution_file(name="ACME", fid="cccccccccccccccc")
    other = contribution_file(number="8445550100", name="Other Co", fid="dddddddddddddddd")
    report = community.import_files(conn, [a, b, c, other], same_meaning=YES)
    assert report.offered == 2 and report.observations_added == 0
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    offers = community.list_offers(conn)
    assert [(o["number"], o["name"], o["files"]) for o in offers] == [
        ("8885550199", "Acme Wireless", 3), ("8445550100", "Other Co", 1)]
    assert set(offers[0]) == {"number", "name", "files", "first_seen", "last_seen"}


def test_an_offer_is_updated_not_duplicated_and_keeps_first_seen():
    conn = fresh(add=False)
    community.import_files(conn, [contribution_file(name="First")], same_meaning=YES)
    conn.execute("UPDATE offers SET first_seen='2000-01-01T00:00:00+00:00'")
    community.import_files(conn, [contribution_file(name="Second"), contribution_file(name="Second", fid="eeeeeeeeeeeeeeee")],
                           same_meaning=YES)
    (offer,) = community.list_offers(conn)
    assert (offer["name"], offer["files"], offer["first_seen"]) == ("Second", 2, "2000-01-01T00:00:00+00:00")


def test_adding_the_company_later_clears_the_offer_and_lets_the_next_update_import():
    conn = fresh(add=False)
    f = contribution_file()
    community.import_files(conn, [f], same_meaning=YES)
    assert len(community.list_offers(conn)) == 1
    db.add_company(conn, "Mine", NUMBER)
    assert community.list_offers(conn) == []
    report = community.import_files(conn, [f], same_meaning=YES)
    assert (report.offered, report.observations_added) == (0, 1)


def test_invalid_files_never_become_offers():
    conn = fresh(add=False)
    community.import_files(conn, [contribution_file(observations=[obs_doc(outcome="robot")])], same_meaning=YES)
    assert community.list_offers(conn) == []


def test_an_existing_company_keeps_its_own_name():
    conn = fresh()
    community.import_files(conn, [contribution_file(name="Other name")], same_meaning=YES)
    assert conn.execute("SELECT name FROM companies").fetchone()[0] == "My Acme"


def test_replay_is_oldest_first_then_shallowest():
    conn = fresh()
    root_old = obs_doc(text="Old root", heard="2026-10-20")
    root_new = obs_doc(text="Totally new root", keys=("1", "3"), heard="2026-10-22")
    child = obs_doc(path="1", text="Sub menu", keys=("1",), heard="2026-10-20")
    report = community.import_files(conn, [contribution_file(observations=[root_new, child, root_old])], same_meaning=NO)
    rows = [(r["path"], r["created_at"]) for r in conn.execute("SELECT path, created_at FROM observations ORDER BY id")]
    assert rows == [("", "2026-10-20"), ("1", "2026-10-20"), ("", "2026-10-22")]
    assert report.results == {"mapped": 2, "changed": 1}
    assert db.get_node(conn, company_id(conn), "")["prompt_text"] == "Totally new root"


def test_replay_is_by_heard_at_across_all_files_whatever_the_file_order():
    conn = fresh()
    newer = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(text="Newer words", heard="2026-10-26", keys=("1", "2"))])
    older = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(text="Older words", heard="2026-10-24")])
    for order in ([newer, older], [older, newer]):
        c = fresh()
        report = community.import_files(c, order, same_meaning=NO)
        assert [r[0] for r in c.execute("SELECT created_at FROM observations ORDER BY id")] == ["2026-10-24", "2026-10-26"]
        assert db.get_node(c, company_id(c), "")["prompt_text"] == "Newer words"
        assert report.results == {"mapped": 1, "changed": 1}


def test_second_import_is_all_already_had():
    conn = fresh()
    f = contribution_file(observations=[obs_doc(), obs_doc(path="1", text="", keys=(), outcome="hold")])
    first = community.import_files(conn, [f], same_meaning=YES)
    second = community.import_files(conn, [f], same_meaning=YES)
    assert (first.observations_added, first.already_had) == (2, 0)
    assert (second.observations_added, second.already_had, second.results) == (0, 2, {})
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # no run for a second import with nothing new


def test_identical_observations_in_two_files_count_once():
    conn = fresh()
    a, b = contribution_file(fid="aaaaaaaaaaaaaaaa"), contribution_file(fid="bbbbbbbbbbbbbbbb")
    report = community.import_files(conn, [a, b], same_meaning=YES)
    assert (report.observations_added, report.already_had) == (1, 1)


def test_no_community_run_when_nothing_new_is_merged():
    conn = fresh()
    community.import_files(conn, [], same_meaning=YES)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_community_never_overwrites_a_crawl_node():
    conn = fresh()
    cid = company_id(conn)
    crawl(conn, cid, "", menu(("1", "2"), "Press a key"))
    crawl(conn, cid, "1", menu(("1",)))
    f = contribution_file(observations=[obs_doc(text="Something else", keys=("1", "3"))])
    assert community.import_files(conn, [f], same_meaning=NO).results == {"disputed": 1}
    root = db.get_node(conn, cid, "")
    assert (root["source"], root["prompt_text"]) == ("crawl", "Press a key")
    assert db.get_node(conn, cid, "1")["status"] == "mapped"


def test_community_never_overwrites_a_phone_node_or_a_stale_one():
    conn = fresh()
    cid = company_id(conn)
    crawl(conn, cid, "", menu(("1", "2")), kind="phone")
    crawl(conn, cid, "1", menu(("1",)))
    conn.execute("UPDATE nodes SET status='stale' WHERE path='1'")
    conn.commit()
    f = contribution_file(observations=[obs_doc(text="Else", keys=("9",)), obs_doc(path="1", text="Else", keys=("9",), heard="2026-10-26")])
    assert community.import_files(conn, [f], same_meaning=NO).results == {"disputed": 2}
    assert db.get_node(conn, cid, "")["source"] == "phone" and db.get_node(conn, cid, "1")["status"] == "stale"


def test_community_confirms_a_crawl_node_when_the_meaning_matches():
    conn = fresh()
    cid = company_id(conn)
    crawl(conn, cid, "", menu(("1", "2"), "Press a key"))
    f = contribution_file(observations=[obs_doc(text="Make a selection", keys=("1", "2"))])
    assert community.import_files(conn, [f], same_meaning=YES).results == {"confirmed": 1}
    root = db.get_node(conn, cid, "")
    assert (root["source"], root["confirmations"]) == ("crawl", 2)


def test_community_fills_unexplored_and_community_nodes_and_changes_the_second():
    conn = fresh()
    cid = company_id(conn)
    f = contribution_file(observations=[
        obs_doc(text="First words", keys=("1", "2"), heard="2026-10-20"),
        obs_doc(text="Reworded entirely", keys=("1", "3"), heard="2026-10-22"),
    ])
    assert community.import_files(conn, [f], same_meaning=NO).results == {"mapped": 1, "changed": 1}
    assert db.get_node(conn, cid, "")["source"] == "community"


def test_community_never_deletes_crawl_branches_or_touches_timing():
    conn = fresh()
    cid = company_id(conn)
    crawl(conn, cid, "", menu(("1", "2")))
    crawl(conn, cid, "2", menu(("1",)))
    crawl(conn, cid, "2.1", Observation("", [], "human", 0.0))
    conn.execute("UPDATE nodes SET source='community' WHERE path=''")  # only the below-check protects 2
    conn.execute("UPDATE nodes SET timing_correction=4.0 WHERE path='2'")
    conn.commit()
    f = contribution_file(observations=[obs_doc(text="Other", keys=("1", "3"), heard="2026-10-20"),
                                        obs_doc(path="2", outcome="invalid_input", text="", keys=(), heard="2026-10-21")])
    report = community.import_files(conn, [f], same_meaning=NO)
    assert report.results == {"disputed": 1, "retry": 1}
    assert db.get_node(conn, cid, "2.1")["status"] == "human"
    assert db.get_node(conn, cid, "2")["timing_correction"] == 4.0
    assert report.observations_added == 1


def test_local_crawl_overwrites_community_and_phone_data():
    conn = fresh()
    cid = company_id(conn)
    community.import_files(conn, [contribution_file()], same_meaning=YES)
    assert crawl(conn, cid, "", menu(("1", "9")), NO) == "changed"
    assert db.get_node(conn, cid, "")["source"] == "crawl"
    crawl(conn, cid, "1", menu(("1",)), kind="phone")
    assert crawl(conn, cid, "1", menu(("2",)), NO) == "changed"
    assert db.get_node(conn, cid, "1")["source"] == "crawl"


def test_local_phone_clip_overwrites_community_data():
    conn = fresh()
    cid = company_id(conn)
    community.import_files(conn, [contribution_file()], same_meaning=YES)
    assert crawl(conn, cid, "", menu(("1", "9")), NO, kind="phone") == "changed"
    assert db.get_node(conn, cid, "")["source"] == "phone"


def test_an_old_file_cannot_change_what_a_newer_observation_set():
    conn = fresh()
    new = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(text="Current", keys=("1", "2"), heard="2026-10-26")])
    old = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(text="Ancient", keys=("5",), heard="2026-01-01")])
    community.import_files(conn, [new], same_meaning=NO)
    report = community.import_files(conn, [old], same_meaning=NO)
    assert report.results == {"disputed": 1}
    assert db.get_node(conn, company_id(conn), "")["prompt_text"] == "Current"


def test_an_undecided_result_keeps_no_row_so_the_next_update_tries_again():
    conn = fresh()
    first = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(text="Alpha words", heard="2026-10-20")])
    second = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(text="Beta words", heard="2026-10-21")])
    community.import_files(conn, [first], same_meaning=YES)
    report = community.import_files(conn, [second], same_meaning=UNSURE)
    assert report.results == {"undecided": 1} and report.observations_added == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    again = community.import_files(conn, [second], same_meaning=YES)
    assert again.results == {"confirmed": 1} and again.already_had == 0 and again.observations_added == 1


def test_the_model_cap_leaves_the_rest_undecided_and_unstored():
    conn = fresh()
    calls = []

    def model(a, b):
        calls.append(1)
        return True

    docs = [obs_doc(text="first words", heard="2026-10-20")] + [
        obs_doc(text=f"other words {i}", heard=f"2026-10-{21 + i}") for i in range(5)]
    report = community.import_files(conn, [contribution_file(observations=docs)], same_meaning=model, max_model_checks=3)
    assert len(calls) == 3
    assert report.results == {"mapped": 1, "confirmed": 3, "undecided": 2}
    assert report.observations_added == 4 and conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4


def test_exact_text_shortcut_makes_no_model_call():
    conn = fresh()
    calls = []

    def model(a, b):
        calls.append((a, b))
        return True

    first = obs_doc(text="Thank you for calling Acme, press 1!", heard="2026-10-20")
    again = obs_doc(text="  thank you   for calling ACME press 1 ", heard="2026-10-21")
    report = community.import_files(conn, [contribution_file(observations=[first, again])], same_meaning=model)
    assert report.results == {"mapped": 1, "confirmed": 1} and calls == []


def test_the_model_cap_is_per_import_not_per_file():
    conn = fresh()
    calls = []

    def model(a, b):
        calls.append(1)
        return True

    a = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(text="one", heard="2026-10-20"),
                                                                obs_doc(text="two", heard="2026-10-21")])
    b = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(text="three", heard="2026-10-22"),
                                                                obs_doc(text="four", heard="2026-10-23")])
    community.import_files(conn, [a, b], same_meaning=model, max_model_checks=2)
    assert len(calls) == 2


def test_a_model_error_is_not_swallowed_by_the_import():
    conn = fresh()

    def boom(a, b):
        raise RuntimeError("model down")

    f = contribution_file(observations=[obs_doc(text="one", heard="2026-10-20"), obs_doc(text="two", heard="2026-10-21")])
    with pytest.raises(RuntimeError, match="model down"):
        community.import_files(conn, [f], same_meaning=boom)


def test_the_new_node_cap_stops_a_company_and_says_so(monkeypatch):
    monkeypatch.setattr(community, "MAX_NEW_NODES", 5)
    conn = fresh()
    keys = ("1", "2", "3", "4", "5", "6", "7")
    docs = [obs_doc(text="root", keys=keys, heard="2026-10-01")] + [
        obs_doc(path=k, text=f"sub {k}", keys=keys[:3], heard=f"2026-10-{2 + i:02d}") for i, k in enumerate(keys)]
    report = community.import_files(conn, [contribution_file(observations=docs)], same_meaning=YES)
    assert report.capped is True and report.truncated is False
    assert report.observations_added == 1  # the root alone already adds 7 nodes
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 8


def test_a_company_below_the_cap_is_not_capped():
    conn = fresh()
    assert community.import_files(conn, [contribution_file()], same_meaning=YES).capped is False


def test_the_cap_for_one_company_does_not_stop_another(monkeypatch):
    monkeypatch.setattr(community, "MAX_NEW_NODES", 1)
    conn = fresh()
    db.add_company(conn, "Other", "+18445550100")
    big = contribution_file(observations=[obs_doc(keys=("1", "2", "3")), obs_doc(path="1", text="x", keys=("1",), heard="2026-10-26")])
    other = contribution_file(number="8445550100", observations=[obs_doc(keys=("1",))])
    report = community.import_files(conn, [big, other], same_meaning=YES)
    assert report.capped is True and report.observations_added == 2


def test_imported_email_is_redacted_as_defence_in_depth():
    conn = fresh()
    f = contribution_file(observations=[obs_doc(text="Write to help@acme.ca")])
    community.import_files(conn, [f], same_meaning=YES)
    assert db.get_node(conn, company_id(conn), "")["prompt_text"] == "Write to [email]"


def test_nothing_imported_for_an_empty_list():
    report = community.import_files(fresh(), [], same_meaning=YES)
    assert (report.files, report.invalid, report.results, report.offered) == (0, [], {}, 0)


def test_export_then_import_round_trips_on_another_machine():
    a, cid = setup()
    crawl(a, cid, "", menu(("1", "2"), "Call 780 555 0199 now"))
    crawl(a, cid, "1", Observation("", [], "hold", 2.0))
    c = build(a, cid)
    b = fresh()
    report = community.import_files(b, [(c.relpath, c.body)], same_meaning=NO)
    assert (report.invalid, report.observations_added) == ([], 2)
    other = company_id(b)
    assert db.get_node(b, other, "")["prompt_text"] == "Call [number] now"
    assert db.get_node(b, other, "1")["status"] == "mapped"


# ---------- approval fixes ----------

def test_a_file_cannot_graft_a_branch_into_the_users_tree():
    conn = fresh()
    cid = company_id(conn)
    crawl(conn, cid, "", menu(("1", "2")))
    graft = obs_doc(path="5", text="Fraud desk, stay on the line", keys=(), outcome="human", heard="2026-10-24")
    report = community.import_files(conn, [contribution_file(observations=[graft])], same_meaning=YES)
    assert report.orphans == 1 and report.observations_added == 0 and report.results == {}
    assert [c["key"] for c in tree.tree_json(conn, cid)["children"]] == ["1", "2"]
    assert db.get_node(conn, cid, "5") is None
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE origin='community'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='community'").fetchone()[0] == 0


def test_a_child_needs_a_parent_that_is_mapped_and_offers_the_key():
    conn = fresh()
    cid = company_id(conn)
    # the parent "1" is only unexplored, and a deeper path has no parent at all
    crawl(conn, cid, "", menu(("1", "2")))
    fills = obs_doc(path="1", text="x", keys=("1",), heard="2026-10-23")
    offered = obs_doc(path="1.1", text="y", keys=(), outcome="hold", heard="2026-10-24")
    not_offered = obs_doc(path="1.5", text="z", keys=(), outcome="hold", heard="2026-10-24")
    report = community.import_files(conn, [contribution_file(observations=[not_offered, offered, fills])], same_meaning=YES)
    assert report.orphans == 1 and report.observations_added == 2
    assert db.get_node(conn, cid, "1")["source"] == "community" and db.get_node(conn, cid, "1.5") is None


def test_an_orphan_is_tried_again_once_its_parent_arrives():
    conn = fresh()
    cid = company_id(conn)
    child = obs_doc(path="1", text="Sub", keys=(), outcome="hold", heard="2026-10-20")
    root = obs_doc(text="Root", keys=("1",), heard="2026-10-22")
    first = community.import_files(conn, [contribution_file(observations=[child])], same_meaning=YES)
    assert first.orphans == 1
    # the root exists now because another file brought it; the same child file is accepted next time
    community.import_files(conn, [contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[root])], same_meaning=YES)
    second = community.import_files(conn, [contribution_file(observations=[child])], same_meaning=YES)
    assert second.orphans == 0 and second.observations_added == 1


TODAY_D = datetime(2026, 10, 25, tzinfo=timezone.utc).date()


def test_far_future_dates_are_ignored_and_cannot_pin_a_node():
    conn = fresh()
    cid = company_id(conn)
    poison = contribution_file(fid="aaaaaaaaaaaaaaaa", day="2099-01-01",
                               observations=[obs_doc(text="Poisoned", heard="2099-01-01")])
    real = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(text="Real words", heard="2026-10-24")])
    report = community.import_files(conn, [poison, real], same_meaning=NO, today=TODAY_D)
    assert report.future == 1 and report.observations_added == 1
    assert db.get_node(conn, cid, "")["prompt_text"] == "Real words"
    later = contribution_file(fid="cccccccccccccccc", observations=[obs_doc(text="Newer reality", keys=("1", "2"), heard="2026-10-25")])
    assert community.import_files(conn, [later], same_meaning=NO, today=TODAY_D).results == {"changed": 1}


def test_tomorrow_is_allowed_for_clock_skew_and_the_day_after_is_not():
    conn = fresh()
    ok = contribution_file(fid="aaaaaaaaaaaaaaaa", observations=[obs_doc(heard="2026-10-26")])
    late = contribution_file(fid="bbbbbbbbbbbbbbbb", observations=[obs_doc(path="1", text="", keys=(), outcome="hold", heard="2026-10-27")])
    report = community.import_files(conn, [ok, late], same_meaning=YES, today=TODAY_D)
    assert (report.observations_added, report.future) == (1, 1)


def test_redaction_is_a_fixed_point_so_the_export_never_raises():
    from zeroout.redact import redact
    tricky = "12345six seven eight nine"
    once = redact(tricky)
    assert redact(once) == once and not community.DIGITS.search(once)
    conn, cid = setup()
    crawl(conn, cid, "", Observation(tricky, [Option("1", tricky)], "menu", 2.0))
    o = build(conn, cid).doc["observations"][0]
    assert not community.DIGITS.search(o["prompt_text"]) and not community.DIGITS.search(o["options"][0]["label"])


def test_a_row_whose_text_still_matches_after_redaction_is_left_out(monkeypatch):
    conn, cid = setup()
    crawl(conn, cid, "", menu())
    monkeypatch.setattr(community, "redact", lambda text: text.replace("Press a key", "pin 1234"))
    assert build(conn, cid) is None


def test_the_company_name_is_redacted_on_export():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Visa card 4111 1111 1111 1111 or help@x.ca", NUMBER)
    crawl(conn, cid, "", menu())
    assert build(conn, cid).doc["name"] == "Visa card [number] or [email]"


def test_an_offer_set_is_replaced_by_each_complete_import():
    conn = fresh(add=False)
    a = contribution_file(number="8885550199", fid="aaaaaaaaaaaaaaaa")
    b = contribution_file(number="8445550100", name="B", fid="bbbbbbbbbbbbbbbb")
    community.import_files(conn, [a, b], same_meaning=YES)
    assert {o["number"] for o in community.list_offers(conn)} == {"8885550199", "8445550100"}
    community.import_files(conn, [a], same_meaning=YES)
    assert {o["number"] for o in community.list_offers(conn)} == {"8885550199"}


def test_a_truncated_download_keeps_the_old_offers():
    conn = fresh(add=False)
    a = contribution_file(number="8885550199", fid="aaaaaaaaaaaaaaaa")
    b = contribution_file(number="8445550100", name="B", fid="bbbbbbbbbbbbbbbb")
    community.import_files(conn, [a, b], same_meaning=YES)
    report = community.import_files(conn, [a], same_meaning=YES, truncated=True)
    assert report.truncated is True and report.capped is False
    assert {o["number"] for o in community.list_offers(conn)} == {"8885550199", "8445550100"}


def test_the_truncated_flag_is_copied_into_the_report():
    assert community.import_files(fresh(), [], same_meaning=YES).truncated is False
    assert community.import_files(fresh(), [], same_meaning=YES, truncated=True).truncated is True


def test_list_offers_limit_and_name_tie_break():
    conn = fresh(add=False)
    files = [contribution_file(number=f"88855501{i:02d}", name="N", fid=f"{i:016x}") for i in range(60)]
    community.import_files(conn, files, same_meaning=YES)
    assert len(community.list_offers(conn)) == 50
    assert len(community.list_offers(conn, limit=5)) == 5
    assert len(community.list_offers(conn, limit=100)) == 60
    tie = fresh(add=False)
    community.import_files(tie, [contribution_file(name="Zed", fid="aaaaaaaaaaaaaaaa"),
                                 contribution_file(name="Alpha", fid="bbbbbbbbbbbbbbbb")], same_meaning=YES)
    assert community.list_offers(tie)[0]["name"] == "Alpha"


@pytest.mark.parametrize("space", [" ", " ", "　"])
def test_company_names_with_unicode_spaces_are_redacted_before_they_collapse(space):
    conn = db.connect(":memory:")
    cid = db.add_company(conn, f"Acme 1{space}800{space}555{space}0199", NUMBER)
    crawl(conn, cid, "", menu())
    name = build(conn, cid).doc["name"]
    assert name == "Acme [number]"


def test_a_prompt_with_unicode_space_numbers_is_redacted_on_export():
    conn, cid = setup()
    crawl(conn, cid, "", Observation("Call 415 555 1234 today", [Option("1", "x")], "menu", 2.0))
    assert build(conn, cid).doc["observations"][0]["prompt_text"] == "Call [number] today"


# ---------- the replays flag stays on this machine (format 1 is unchanged) ----------

def _flagged_and_plain():
    """Two identical databases; in one, the stored options also carry B's replays flag."""
    built = []
    for flagged in (False, True):
        conn, cid = setup()
        crawl(conn, cid, "", menu(("1", "2")), created="2026-10-20 10:00:00")
        if flagged:
            raw = json.loads(conn.execute("SELECT raw_json FROM observations").fetchone()["raw_json"])
            raw["options"] = [{**o, "replays": o["key"] == "2"} for o in raw["options"]]
            conn.execute("UPDATE observations SET raw_json = ?", (json.dumps(raw),))
            conn.commit()
        built.append(build(conn, cid))
    return built


def test_replays_flag_is_not_exported_and_the_file_is_byte_identical():
    plain, flagged = _flagged_and_plain()
    assert plain is not None and flagged is not None
    assert flagged.body == plain.body
    assert b"replays" not in flagged.body
    assert all(set(o) == {"key", "label"} for ob in flagged.doc["observations"] for o in ob["options"])


def test_replays_flag_does_not_reach_the_outbox(tmp_path):
    from zeroout import github
    _, flagged = _flagged_and_plain()
    sent = github._outbox(flagged, "o/r", tmp_path, "no gh")
    assert sent.outbox is not None and b"replays" not in sent.outbox.read_bytes()


def test_a_shared_file_with_a_replays_field_is_refused_as_before():
    _, flagged = _flagged_and_plain()
    doc = json.loads(flagged.body)
    doc["observations"][0]["options"][0]["replays"] = False
    problems = check(flagged.relpath, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
    assert any("unknown field" in p and "replays" in p for p in problems)
