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
    assert t["options"] == [{"key": "2", "label": "opt2"}, {"key": "1", "label": "opt1"}]
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


def test_nano_same_meaning_falls_back_to_false():
    s = SimpleNamespace(model_nano="nano")
    assert tree.nano_same_meaning(s, chat=lambda *a: None)("a", "b") is False
    assert tree.nano_same_meaning(s, chat=lambda *a: {"same": "yes"})("a", "b") is False
    assert tree.nano_same_meaning(s, chat=lambda *a: {"same": False})("a", "b") is False
    assert tree.nano_same_meaning(s, chat=lambda *a: {})("a", "b") is False


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
