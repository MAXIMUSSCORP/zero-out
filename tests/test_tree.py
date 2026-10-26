import json
from types import SimpleNamespace

from zeroout import db, tree
from zeroout.models import Observation, Option

YES = lambda a, b: True
NO = lambda a, b: False


def setup():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+15550001")
    rid = db.start_run(conn, cid, "crawl", "t")
    tree.ensure_root(conn, cid)
    return conn, cid, rid


def menu(text="Press 1 or 2", keys=("1", "2")):
    return Observation(text, [Option(k, f"opt{k}") for k in keys], "menu", 3.0)


def m(conn, cid, rid, path, obs, same=YES):
    return tree.merge(conn, cid, path, obs, rid, f"/a/{path}.wav", same)


def n_obs(conn, cid):
    return conn.execute("SELECT COUNT(*) FROM observations WHERE company_id=?", (cid,)).fetchone()[0]


def status(conn, cid, path):
    return db.get_node(conn, cid, path)["status"]


def test_unexplored_to_mapped_creates_children():
    conn, cid, rid = setup()
    assert m(conn, cid, rid, "", menu()) == "mapped"
    assert status(conn, cid, "") == "mapped"
    assert status(conn, cid, "1") == "unexplored"
    assert status(conn, cid, "2") == "unexplored"
    node = db.get_node(conn, cid, "")
    assert node["confirmations"] == 1 and node["prompt_seconds"] == 3.0
    row = conn.execute("SELECT * FROM observations").fetchone()
    assert row["audio_url"] == "/a/.wav"
    assert json.loads(row["raw_json"])["prompt_text"] == "Press 1 or 2"


def test_nested_child_paths():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu(keys=("3", "#")))
    assert status(conn, cid, "1.3") == "unexplored"
    assert status(conn, cid, "1.#") == "unexplored"


def test_same_meaning_confirms():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    assert m(conn, cid, rid, "", menu("Different words")) == "confirmed"
    node = db.get_node(conn, cid, "")
    assert node["confirmations"] == 2
    assert node["status"] == "mapped"
    assert n_obs(conn, cid) == 2


def test_same_meaning_not_called_when_keys_differ():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())

    def boom(a, b):
        raise AssertionError("should not be called")

    assert m(conn, cid, rid, "", menu(keys=("1", "2", "3")), boom) == "changed"


def test_change_marks_descendants_stale():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("hold", [], "hold", 1.0))
    assert m(conn, cid, rid, "", menu("New prompt"), NO) == "changed"
    assert status(conn, cid, "") == "changed"
    assert status(conn, cid, "1") == "stale"
    assert db.get_node(conn, cid, "")["prompt_text"] == "New prompt"
    assert db.get_node(conn, cid, "")["confirmations"] == 1
    assert n_obs(conn, cid) == 3


def test_stale_only_hits_descendants():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu(keys=("1", "2", "11")))
    m(conn, cid, rid, "1", menu(keys=("1",)))
    m(conn, cid, rid, "1.1", Observation("h", [], "hold"))
    m(conn, cid, rid, "11", Observation("h", [], "hold"))
    m(conn, cid, rid, "1", menu("changed text", keys=("1",)), NO)
    assert status(conn, cid, "1.1") == "stale"
    assert status(conn, cid, "11") == "mapped"
    assert status(conn, cid, "2") == "unexplored"


def test_stale_node_reconfirmed_becomes_fresh():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("hi", [], "hold"))
    m(conn, cid, rid, "", menu("x"), NO)
    assert status(conn, cid, "1") == "stale"
    assert m(conn, cid, rid, "1", Observation("hi again", [], "hold")) == "confirmed"
    assert status(conn, cid, "1") == "mapped"
    assert db.get_node(conn, cid, "1")["confirmations"] == 2


def test_stale_terminal_reconfirmed_restores_terminal_status():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", Observation("Enter your account number", [], "needs_input"))
    m(conn, cid, rid, "", menu("x"), NO)
    assert status(conn, cid, "2") == "stale"
    assert m(conn, cid, rid, "2", Observation("Enter your account number", [], "needs_input")) == "confirmed"
    assert status(conn, cid, "2") == "needs_input"


def test_changed_root_with_new_key_creates_child():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    assert m(conn, cid, rid, "", menu(keys=("1", "2", "3")), YES) == "changed"
    assert status(conn, cid, "") == "changed"
    assert status(conn, cid, "3") == "unexplored"
    assert json.loads(db.get_node(conn, cid, "")["options_json"])[-1]["key"] == "3"


def test_changed_node_can_be_reconfirmed():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "", menu("new"), NO)
    assert m(conn, cid, rid, "", menu("new")) == "confirmed"
    assert status(conn, cid, "") == "mapped"


def test_invalid_input_bumps_correction():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    before = n_obs(conn, cid)
    inv = Observation("invalid", [], "invalid_input")
    assert m(conn, cid, rid, "1", inv) == "retry"
    assert db.get_node(conn, cid, "1")["timing_correction"] == 2.0
    assert m(conn, cid, rid, "1", inv) == "retry"
    assert db.get_node(conn, cid, "1")["timing_correction"] == 4.0
    assert n_obs(conn, cid) == before


def test_invalid_input_creates_missing_node():
    conn, cid, rid = setup()
    assert m(conn, cid, rid, "9", Observation("x", [], "invalid_input")) == "retry"
    node = db.get_node(conn, cid, "9")
    assert node["status"] == "unexplored" and node["timing_correction"] == 2.0


def test_human_and_needs_input_are_terminal():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu(keys=("1", "2", "3")))
    assert m(conn, cid, rid, "1", Observation("agent", [], "human")) == "terminal"
    assert status(conn, cid, "1") == "human"
    ni = Observation("enter account", [Option("1", "x")], "needs_input")
    assert m(conn, cid, rid, "2", ni) == "terminal"
    assert status(conn, cid, "2") == "needs_input"
    assert m(conn, cid, rid, "3", Observation("say it", [], "speech_menu")) == "terminal"
    assert status(conn, cid, "3") == "speech_menu"
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE path LIKE '2.%'").fetchone()[0] == 0
    assert json.loads(db.get_node(conn, cid, "2")["options_json"])[0]["key"] == "1"


def test_hold_voicemail_hangup_mapped_without_children():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu(keys=("1", "2", "3")))
    for k, o in (("1", "hold"), ("2", "voicemail"), ("3", "hangup")):
        assert m(conn, cid, rid, k, Observation("t", [Option("9", "x")], o)) == "mapped"
        assert status(conn, cid, k) == "mapped"
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 4


def test_terminal_confirmed_keeps_status():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("agent", [], "human"))
    assert m(conn, cid, rid, "1", Observation("agent", [], "human")) == "confirmed"
    assert status(conn, cid, "1") == "human"


def test_unexplored_ordering_and_depth():
    conn, cid, rid = setup()
    assert tree.unexplored(conn, cid, 4) == [""]
    m(conn, cid, rid, "", menu(keys=("2", "1", "10")))
    m(conn, cid, rid, "2", menu(keys=("5",)))
    m(conn, cid, rid, "2.5", menu(keys=("7",)))
    assert tree.unexplored(conn, cid, 1) == ["1", "10"]
    assert tree.unexplored(conn, cid, 3) == ["1", "10", "2.5.7"]
    assert tree.unexplored(conn, cid, 0) == []


def test_unexplored_includes_stale_and_orders_by_depth():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu(keys=("4",)))
    m(conn, cid, rid, "1.4", Observation("h", [], "hold"))
    m(conn, cid, rid, "", menu("x"), NO)
    assert tree.unexplored(conn, cid, 4) == ["1", "2", "1.4"]


def test_tree_json_none_without_root():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "A", "1")
    assert tree.tree_json(conn, cid) is None


def test_tree_json_nests():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu(keys=("2", "1")))
    m(conn, cid, rid, "1", Observation("hold", [], "hold"))
    t = tree.tree_json(conn, cid)
    assert t["path"] == "" and t["key"] == "" and t["label"] == ""
    assert t["status"] == "mapped" and t["outcome"] == "menu"
    assert t["options"] == [{"key": "2", "label": "opt2", "replays": False}, {"key": "1", "label": "opt1", "replays": False}]
    assert [c["path"] for c in t["children"]] == ["1", "2"]
    c1 = t["children"][0]
    assert c1["key"] == "1" and c1["label"] == "opt1" and c1["status"] == "mapped"
    assert c1["confirmations"] == 1 and c1["children"] == []
    assert t["children"][1]["status"] == "unexplored"
    assert t["children"][1]["options"] == []


def test_nano_same_meaning():
    s = SimpleNamespace(model_nano="nano")
    calls = []

    def chat(settings, model, messages):
        calls.append((settings, model, messages))
        return {"same": True}

    f = tree.nano_same_meaning(s, chat=chat)
    assert f("a", "b") is True
    assert calls[0][0] is s and calls[0][1] == "nano"
    assert "a" in json.dumps(calls[0][2]) and "b" in json.dumps(calls[0][2])


def test_change_never_requeues_human_descendants():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", Observation("Hello, this is Sam", [], "human", 1.0))
    m(conn, cid, rid, "", menu("New prompt"), NO)
    assert status(conn, cid, "2") == "human"
    assert "2" not in tree.unexplored(conn, cid, 4)


def test_observation_records_merge_result():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "", menu("New"), NO)
    rows = conn.execute("SELECT result FROM observations ORDER BY id").fetchall()
    assert [r["result"] for r in rows] == ["mapped", "confirmed", "changed"]


def test_nano_same_meaning_none_when_undecided():
    s = SimpleNamespace(model_nano="nano")
    for out in (None, {"same": "yes"}, {}, [1]):
        assert tree.nano_same_meaning(s, chat=lambda *a, out=out: out)("a", "b") is None
    assert tree.nano_same_meaning(s, chat=lambda *a: {"same": False})("a", "b") is False


def test_nano_same_meaning_none_on_exception():
    s = SimpleNamespace(model_nano="nano")

    def boom(*a):
        raise RuntimeError("down")

    assert tree.nano_same_meaning(s, chat=boom)("a", "b") is None


def test_undecided_same_meaning_leaves_node_untouched():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("hold", [], "hold"))
    before = dict(db.get_node(conn, cid, ""))
    assert m(conn, cid, rid, "", menu("other words"), lambda a, b: None) == "undecided"
    assert dict(db.get_node(conn, cid, "")) == before
    assert status(conn, cid, "1") == "mapped"
    rows = conn.execute("SELECT result FROM observations ORDER BY id").fetchall()
    assert [r["result"] for r in rows] == ["mapped", "mapped", "undecided"]


def test_same_meaning_called_without_lock_held():
    import threading

    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    got = []

    def grab():
        got.append(db.LOCK.acquire(timeout=1))
        db.LOCK.release()

    def check(a, b):
        t = threading.Thread(target=grab)
        t.start()
        t.join()
        return True

    m(conn, cid, rid, "", menu("x"), check)
    assert got == [True]


def test_merge_holds_lock_for_whole_transaction():
    import threading

    conn, cid, rid = setup()
    seen = []
    done = threading.Event()

    def other():
        seen.append(db.LOCK.acquire(timeout=0.05))


    class Spy:
        def __init__(self, c):
            self.c = c

        def execute(self, *a):
            if "UPDATE observations" in a[0]:
                t = threading.Thread(target=other)
                t.start()
                t.join()
            return self.c.execute(*a)

        def __getattr__(self, n):
            return getattr(self.c, n)

    tree.merge(Spy(conn), cid, "", menu(), rid, "", YES)
    assert seen == [False]


def test_removed_keys_delete_subtree_kept_keys_stale():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu(keys=("1", "2")))
    m(conn, cid, rid, "1", menu(keys=("5",)))
    m(conn, cid, rid, "1.5", Observation("h", [], "hold"))
    m(conn, cid, rid, "2", Observation("h", [], "hold"))
    obs_before = n_obs(conn, cid)
    assert m(conn, cid, rid, "", menu("new", keys=("2", "3")), NO) == "changed"
    assert db.get_node(conn, cid, "1") is None and db.get_node(conn, cid, "1.5") is None
    assert status(conn, cid, "2") == "stale"
    assert status(conn, cid, "3") == "unexplored"
    assert n_obs(conn, cid) == obs_before + 1
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE path='1.5'").fetchone()[0] == 1


def test_changed_to_terminal_deletes_all_descendants():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("h", [], "hold"))
    m(conn, cid, rid, "", Observation("say it", [], "speech_menu"), NO)
    assert db.get_node(conn, cid, "1") is None and db.get_node(conn, cid, "2") is None


def test_timing_correction_capped_at_ten():
    conn, cid, rid = setup()
    inv = Observation("invalid", [], "invalid_input")
    for _ in range(8):
        m(conn, cid, rid, "1", inv)
    assert db.get_node(conn, cid, "1")["timing_correction"] == 10.0


# --- source: phone clips never overwrite an automatic call ---

def src(conn, cid, path):
    return db.get_node(conn, cid, path)["source"]


def mp(conn, cid, rid, path, obs, same=YES):
    return tree.merge(conn, cid, path, obs, rid, None, same, source="phone")


def test_phone_fills_unexplored_and_marks_source_phone():
    conn, cid, rid = setup()
    assert mp(conn, cid, rid, "", menu()) == "mapped"
    assert src(conn, cid, "") == "phone"
    assert src(conn, cid, "1") == "crawl"  # a child nobody has heard yet
    assert conn.execute("SELECT audio_url FROM observations").fetchone()[0] is None


def test_phone_confirms_crawl_and_keeps_crawl_source():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    assert mp(conn, cid, rid, "", menu()) == "confirmed"
    assert src(conn, cid, "") == "crawl"
    assert db.get_node(conn, cid, "")["confirmations"] == 2


def test_phone_disputing_crawl_changes_nothing_below():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu(keys=("1",)))
    before = db.get_node(conn, cid, "")["prompt_text"]
    for obs, same in ((menu(keys=("1", "3")), YES), (menu(), NO), (menu(), lambda a, b: None)):
        assert mp(conn, cid, rid, "", obs, same) == "disputed"
    node = db.get_node(conn, cid, "")
    assert (node["prompt_text"], node["status"], node["source"], node["confirmations"]) == (before, "mapped", "crawl", 1)
    assert status(conn, cid, "1") == "mapped"
    assert status(conn, cid, "2") == "unexplored"
    rows = conn.execute("SELECT result FROM observations WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    assert [r[0] for r in rows][-3:] == ["disputed"] * 3


def test_phone_overwrites_stale_crawl_node_and_phone_node():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    conn.execute("UPDATE nodes SET status='stale' WHERE path=''")
    assert mp(conn, cid, rid, "", menu(keys=("1", "3"))) == "changed"
    assert src(conn, cid, "") == "phone"
    assert mp(conn, cid, rid, "", menu(keys=("1", "4"))) == "changed"  # phone over phone


def test_crawl_overwrites_a_phone_node_and_takes_the_source_back():
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", menu())
    assert m(conn, cid, rid, "", menu(keys=("1", "3"))) == "changed"
    assert src(conn, cid, "") == "crawl"
    mp(conn, cid, rid, "1", menu(keys=("1",)))
    assert m(conn, cid, rid, "1", menu(keys=("1",))) == "confirmed"
    assert src(conn, cid, "1") == "crawl"


def test_phone_clip_cannot_delete_crawl_subtrees_below_a_phone_node():
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", menu())  # the phone maps the root
    m(conn, cid, rid, "2", menu(keys=("1",)))  # a crawl maps 2 ...
    m(conn, cid, rid, "2.1", Observation("", [], "human", 0.0))  # ... and finds a person at 2.1
    assert mp(conn, cid, rid, "", menu(keys=("1", "3"))) == "disputed"
    assert status(conn, cid, "2") == "mapped" and status(conn, cid, "2.1") == "human"
    root = db.get_node(conn, cid, "")
    assert root["source"] == "phone" and "opt2" in root["options_json"] and status(conn, cid, "") == "mapped"
    assert db.get_node(conn, cid, "3") is None
    assert conn.execute("SELECT result FROM observations ORDER BY id DESC").fetchone()[0] == "disputed"


def test_phone_clip_on_a_stale_crawl_node_cannot_delete_crawl_subtrees():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", menu(keys=("1",)))
    conn.execute("UPDATE nodes SET status='stale' WHERE path=''")
    assert mp(conn, cid, rid, "", menu(keys=("1", "3"))) == "disputed"
    assert status(conn, cid, "2") == "mapped" and src(conn, cid, "") == "crawl"


def test_phone_change_is_still_allowed_when_nothing_crawled_lies_below():
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", menu())
    mp(conn, cid, rid, "2", menu(keys=("1",)))  # phone below phone
    assert mp(conn, cid, rid, "", menu(keys=("1", "3"))) == "changed"
    assert db.get_node(conn, cid, "2") is None
    assert status(conn, cid, "3") == "unexplored"


def test_phone_invalid_input_leaves_crawl_timing_alone():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu(keys=("1",)))
    bad = Observation("", [], "invalid_input", 0.0)
    assert mp(conn, cid, rid, "1", bad) == "retry"
    assert (db.get_node(conn, cid, "1")["timing_correction"] or 0) == 0
    assert m(conn, cid, rid, "1", bad) == "retry"  # a crawl's own invalid key still counts
    assert db.get_node(conn, cid, "1")["timing_correction"] == 2.0


def test_phone_invalid_input_on_a_missing_node_writes_nothing():
    conn, cid, rid = setup()
    assert mp(conn, cid, rid, "9", Observation("", [], "invalid_input", 0.0)) == "retry"
    assert db.get_node(conn, cid, "9") is None


def test_stale_crawl_descendant_still_protects_against_a_phone_key_change():
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", menu(keys=("1",)))
    conn.execute("UPDATE nodes SET status='stale' WHERE path='2'")
    assert mp(conn, cid, rid, "", menu(keys=("1", "3"))) == "disputed"
    assert status(conn, cid, "2") == "stale" and src(conn, cid, "2") == "crawl"
    assert db.get_node(conn, cid, "3") is None


def test_phone_same_keys_different_meaning_over_crawled_descendants_is_changed():
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", menu(keys=("1",)))
    m(conn, cid, rid, "2.1", Observation("", [], "human", 0.0))
    assert mp(conn, cid, rid, "", menu(text="A different greeting"), NO) == "changed"
    assert status(conn, cid, "") == "changed"
    assert status(conn, cid, "2") == "stale"  # marked for a re-check, not deleted
    assert status(conn, cid, "2.1") == "human"  # human nodes are only re-checked by Connect me


# --- source: community files protect everything the user mapped ---

def mc(conn, cid, rid, path, obs, same=YES, heard_at=None):
    return tree.merge(conn, cid, path, obs, rid, None, same, source="community",
                      origin="community", uid=None, heard_at=heard_at)


def test_community_cannot_rewrite_a_phone_tree():
    """Reviewer case: a phone root with 1 and 2 and a phone child, then a community root that only offers 9."""
    conn, cid, rid = setup()
    mp(conn, cid, rid, "", Observation("Cards press 1, loans press 2", [Option("1", "cards"), Option("2", "loans")], "menu", 4.0))
    mp(conn, cid, rid, "1", menu(keys=("1",)))
    before = [tuple(r) for r in conn.execute("SELECT * FROM nodes ORDER BY path")]
    fraud = Observation("For fraud press 9", [Option("9", "fraud")], "menu", 120.0)
    assert mc(conn, cid, rid, "", fraud, NO) == "disputed"
    assert [tuple(r) for r in conn.execute("SELECT * FROM nodes ORDER BY path")] == before
    assert conn.execute("SELECT result FROM observations ORDER BY id DESC").fetchone()[0] == "disputed"


def test_community_over_stale_user_data_is_disputed_too():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    mp(conn, cid, rid, "1", menu(keys=("1",)))
    conn.execute("UPDATE nodes SET status='stale' WHERE path IN ('', '1')")
    assert mc(conn, cid, rid, "", menu(keys=("9",)), NO) == "disputed"
    assert mc(conn, cid, rid, "1", menu(keys=("9",)), NO) == "disputed"
    assert status(conn, cid, "") == "stale" and src(conn, cid, "") == "crawl" and src(conn, cid, "1") == "phone"


def test_community_confirms_user_data_but_never_takes_the_source():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    mp(conn, cid, rid, "1", menu(keys=("1",)))
    assert mc(conn, cid, rid, "", menu(), YES) == "confirmed"
    assert mc(conn, cid, rid, "1", menu(keys=("1",)), YES) == "confirmed"
    assert (src(conn, cid, ""), src(conn, cid, "1")) == ("crawl", "phone")
    assert db.get_node(conn, cid, "")["confirmations"] == 2
    assert mc(conn, cid, rid, "", menu(), (lambda a, b: None)) == "disputed"


def test_community_over_community_may_change_and_phone_and_crawl_win_over_it():
    conn, cid, rid = setup()
    mc(conn, cid, rid, "", menu())
    assert mc(conn, cid, rid, "", menu(keys=("1", "3")), NO) == "changed"
    assert src(conn, cid, "") == "community"
    assert mp(conn, cid, rid, "", menu(keys=("1", "4")), NO) == "changed"
    assert src(conn, cid, "") == "phone"
    mc2 = mc(conn, cid, rid, "", menu(keys=("1", "5")), NO)
    assert mc2 == "disputed"
    assert m(conn, cid, rid, "", menu(keys=("1", "6")), NO) == "changed"
    assert src(conn, cid, "") == "crawl"


def test_community_change_over_phone_descendants_is_disputed():
    conn, cid, rid = setup()
    mc(conn, cid, rid, "", menu())
    mp(conn, cid, rid, "2", menu(keys=("1",)))
    assert mc(conn, cid, rid, "", menu(keys=("1", "3")), NO) == "disputed"
    assert status(conn, cid, "2") == "mapped" and src(conn, cid, "2") == "phone"
    assert db.get_node(conn, cid, "3") is None


def test_community_undecided_keeps_no_row_so_it_is_tried_again():
    conn, cid, rid = setup()
    mc(conn, cid, rid, "", menu(), heard_at="2026-10-01")
    before = n_obs(conn, cid)
    assert mc(conn, cid, rid, "", menu(), lambda a, b: None, heard_at="2026-10-02") == "undecided"
    assert n_obs(conn, cid) == before
    assert mp(conn, cid, rid, "", menu(), lambda a, b: None) == "undecided"  # others keep theirs
    assert n_obs(conn, cid) == before + 1


def test_older_community_observation_may_confirm_but_never_change():
    conn, cid, rid = setup()
    mc(conn, cid, rid, "", menu(), heard_at="2026-10-10")
    assert mc(conn, cid, rid, "", menu(keys=("1", "3")), NO, heard_at="2026-10-01") == "disputed"
    assert db.get_node(conn, cid, "")["options_json"].count("opt2") == 1
    assert mc(conn, cid, rid, "", menu(), YES, heard_at="2026-10-02") == "confirmed"
    assert mc(conn, cid, rid, "", menu(keys=("1", "3")), NO, heard_at="2026-10-10") == "changed"  # equal is not older
    assert mc(conn, cid, rid, "", menu(keys=("1", "4")), NO, heard_at="2026-10-11") == "changed"


def test_older_is_judged_against_local_observations_too():
    conn, cid, rid = setup()
    mc(conn, cid, rid, "", menu(), heard_at="2026-10-01")
    conn.execute("INSERT INTO observations (company_id, path, raw_json, created_at, origin) "
                 "VALUES (?, '', '{}', '2026-10-20T10:00:00+00:00', 'local')", (cid,))
    assert mc(conn, cid, rid, "", menu(keys=("7",)), NO, heard_at="2026-10-15") == "disputed"


def menu_with(labels: dict):
    return Observation("press", [Option(k, v) for k, v in labels.items()], "menu", 3.0)


def test_unexplored_skips_options_that_replay_a_menu():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu_with({"1": "billing", "*": "To repeat this information", "#": "Return to the previous menu"}))
    assert tree.unexplored(conn, cid, 4) == ["1"]


def test_unexplored_skips_everything_under_a_replay_option():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu_with({"1": "billing", "*": "repeat options"}))
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '*.3', 'unexplored')", (cid,))
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '1.2', 'unexplored')", (cid,))
    assert tree.unexplored(conn, cid, 4) == ["1", "1.2"]


def test_a_star_that_is_a_real_choice_is_still_explored():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu_with({"1": "billing", "*": "Returns and exchanges"}))
    assert tree.unexplored(conn, cid, 4) == ["*", "1"]


def test_stale_replay_option_is_not_revisited():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu_with({"*": "To repeat this information"}))
    conn.execute("UPDATE nodes SET status = 'stale' WHERE company_id = ? AND path = '*'", (cid,))
    assert tree.unexplored(conn, cid, 4) == []


def test_tree_json_marks_replay_options():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu_with({"1": "billing", "*": "To repeat this information"}))
    kids = {c["key"]: c for c in tree.tree_json(conn, cid)["children"]}
    assert kids["*"]["replays"] is True and kids["1"]["replays"] is False
    assert tree.tree_json(conn, cid)["replays"] is False


COSTCO_MENU = ("Thank you for calling Costco. For billing questions, press 1. For Costco Travel, press 5. "
               "To repeat this information, press star.")


def costco_root(conn, cid, rid, labels):
    obs = Observation(COSTCO_MENU, [Option(k, v) for k, v in labels.items()], "menu", 3.0)
    m(conn, cid, rid, "", obs)


def test_tree_json_fills_bare_labels_from_the_parent_prompt():
    conn, cid, rid = setup()
    costco_root(conn, cid, rid, {"1": "one", "5": "", "7": "press 7", "*": "star"})
    t = tree.tree_json(conn, cid)
    assert {c["key"]: c["label"] for c in t["children"]} == {
        "1": "Billing questions", "5": "Costco Travel", "7": "Option 7", "*": "Repeat this information"}
    assert {o["key"]: o["label"] for o in t["options"]} == {c["key"]: c["label"] for c in t["children"]}
    assert db.get_node(conn, cid, "")["options_json"].count('"label": ""') == 1  # the stored data is untouched


def test_a_star_labelled_only_star_is_recognized_as_a_replay_from_its_prompt_sentence():
    conn, cid, rid = setup()
    costco_root(conn, cid, rid, {"1": "one", "*": "star"})
    assert tree.unexplored(conn, cid, 4) == ["1"]
    assert {c["key"]: c["replays"] for c in tree.tree_json(conn, cid)["children"]} == {"1": False, "*": True}


TRAVEL = ("Thank you for calling Costco Travel. For rental cars, press 1. For cruises, press 2. "
          "For specialty travel or guided vacations. Press 3. For travel within the United States. Press 4. "
          "For Canada vacation packages. Press 5. For all other international travel. Press 6. "
          "For travel insurance or if you are in need of in-travel or post-travel assistance. Press 7. "
          "To repeat these options, press the star key.")


def test_shifted_stored_labels_are_corrected_from_the_transcript_at_read_time():
    conn, cid, rid = setup()
    stored = {"1": "rental cars", "2": "cruises", "3": "travel within the United States",
              "4": "Canada vacation packages", "5": "all other international travel",
              "6": "travel insurance or if", "7": "", "*": "star"}
    m(conn, cid, rid, "", menu("Press 5", ("5",)))
    m(conn, cid, rid, "5", Observation(TRAVEL, [Option(k, v) for k, v in stored.items()], "menu", 3.0))
    node = next(c for c in tree.tree_json(conn, cid)["children"] if c["key"] == "5")
    assert {c["key"]: c["label"] for c in node["children"]} == {
        "1": "Rental cars", "2": "Cruises", "3": "Specialty travel or guided vacations",
        "4": "Travel within the United States", "5": "Canada vacation packages",
        "6": "All other international travel",
        "7": "Travel insurance or if you are in need of in-travel or post-travel assistance",
        "*": "Repeat these options"}
    assert {c["key"] for c in node["children"] if c["replays"]} == {"*"}
    assert "5.*" not in tree.unexplored(conn, cid, 4) and "5.3" in tree.unexplored(conn, cid, 4)


def test_the_derived_label_decides_a_replay_and_a_shifted_stored_label_cannot_hide_a_real_option():
    conn, cid, rid = setup()
    rental = ("If you need assistance, please press pound to continue. For bookings, press 1. "
              "To repeat these options, press the star key. To return to the previous, press the pound key.")
    m(conn, cid, rid, "", Observation(rental, [Option("#", "Return to the previous menu"),
                                               Option("1", "To repeat these options"), Option("*", "")], "menu", 3.0))
    kids = {c["key"]: c for c in tree.tree_json(conn, cid)["children"]}
    assert kids["#"]["replays"] is True and kids["*"]["replays"] is True and kids["1"]["replays"] is False
    assert tree.unexplored(conn, cid, 4) == ["1"]


def flagged(conn, cid, rid, text, opts, path=""):
    m(conn, cid, rid, path, Observation(text, [Option(k, lab, flag) for k, lab, flag in opts], "menu", 3.0))


def test_the_replays_flag_round_trips_through_merge_options_json_and_tree_json():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. To go back press 2.",
            [("1", "Billing", False), ("2", "Go back", True), ("3", "Plain", None)])
    stored = json.loads(db.get_node(conn, cid, "")["options_json"])
    assert stored == [{"key": "1", "label": "Billing", "replays": False},
                      {"key": "2", "label": "Go back", "replays": True}, {"key": "3", "label": "Plain"}]
    t = tree.tree_json(conn, cid)
    assert {o["key"]: o["replays"] for o in t["options"]} == {"1": False, "2": True, "3": False}
    assert {c["key"]: c["replays"] for c in t["children"]} == {"1": False, "2": True, "3": False}


def test_a_true_flag_on_a_real_option_does_not_hide_it():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. For sales press 2.", [("1", "Billing", True), ("2", "Sales", False)])
    assert tree.unexplored(conn, cid, 4) == ["1", "2"]
    assert {c["key"]: c["replays"] for c in tree.tree_json(conn, cid)["children"]} == {"1": False, "2": False}


def test_a_true_flag_on_a_navigation_label_is_a_replay_even_when_the_words_are_silent():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. Press star.", [("1", "Billing", False), ("*", "Repeat these options", True)])
    assert tree.unexplored(conn, cid, 4) == ["1"]


def test_a_false_flag_cannot_unmark_a_replay_the_words_show():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. To repeat these options, press the star key.",
            [("1", "Billing", False), ("*", "Repeat", False)])
    assert tree.unexplored(conn, cid, 4) == ["1"]


def test_flags_that_mark_every_option_are_ignored():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. For sales press 2.", [("1", "Billing", True), ("2", "Repeat", True)])
    assert tree.unexplored(conn, cid, 4) == ["1", "2"]


def test_flags_that_mark_more_than_half_of_a_menu_are_not_trusted_but_half_or_less_are():
    def opts(*flags):
        return [{"key": str(i + 1), "label": "x", "replays": f} for i, f in enumerate(flags)]
    assert tree._trusted_flags(opts(True, True, True, False)) == {"1": None, "2": None, "3": None, "4": None}
    assert tree._trusted_flags(opts(True, True)) == {"1": None, "2": None}
    assert tree._trusted_flags(opts(True)) == {"1": None}
    assert tree._trusted_flags(opts(True, True, False, False)) == {"1": True, "2": True, "3": False, "4": False}
    assert tree._trusted_flags(opts(True, False)) == {"1": True, "2": False}
    assert tree._trusted_flags(opts(None, False, None)) == {"1": None, "2": False, "3": None}
    assert tree._trusted_flags([]) == {}


def test_a_flag_never_matters_where_the_transcript_gives_the_key_words():
    conn, cid, rid = setup()
    text = ("To start over with a new booking, press 1. For questions about the main menu items on our website, "
            "press 2. For sales, press 3.")
    flagged(conn, cid, rid, text, [("1", "Start over", True), ("2", "Main menu items", True), ("3", "Sales", False)])
    assert tree.unexplored(conn, cid, 4) == ["1", "2", "3"]


def test_a_confirming_merge_refreshes_the_stored_flags_and_a_bad_one_does_not_outlive_it():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. For sales press 2.", [("1", "Repeat", True), ("2", "Sales", False)])
    assert json.loads(db.get_node(conn, cid, "")["options_json"])[0]["replays"] is True
    assert m(conn, cid, rid, "", Observation("For billing press 1. For sales press 2.",
                                             [Option("1", "Billing", False), Option("2", "Sales", None)], "menu", 3.0)) == "confirmed"
    stored = json.loads(db.get_node(conn, cid, "")["options_json"])
    assert [o.get("replays") for o in stored] == [False, False]
    assert tree.unexplored(conn, cid, 4) == ["1", "2"]


def test_a_confirming_merge_with_no_flags_keeps_the_stored_ones():
    conn, cid, rid = setup()
    flagged(conn, cid, rid, "For billing press 1. To go back press 2.", [("1", "Billing", False), ("2", "Back", True)])
    m(conn, cid, rid, "", Observation("For billing press 1. To go back press 2.", [Option("1", "Billing"), Option("2", "Back")], "menu", 3.0))
    assert [o.get("replays") for o in json.loads(db.get_node(conn, cid, "")["options_json"])] == [False, True]


def test_with_no_flag_the_word_rules_still_decide():
    conn, cid, rid = setup()
    text = "For billing press 1. To repeat these options, press the star key. For the main menu, press 3."
    flagged(conn, cid, rid, text, [("1", "Billing", None), ("*", "Repeat", None), ("3", "x", None)])
    kids = {c["key"]: c["replays"] for c in tree.tree_json(conn, cid)["children"]}
    assert kids == {"1": False, "*": True, "3": True}
    assert tree.unexplored(conn, cid, 4) == ["1"]


def test_a_flag_survives_a_confirming_merge():
    conn, cid, rid = setup()
    for _ in range(2):
        flagged(conn, cid, rid, "For billing press 1. To repeat press star.", [("1", "Billing", False), ("*", "Repeat", True)])
    assert [o.get("replays") for o in json.loads(db.get_node(conn, cid, "")["options_json"])] == [False, True]
