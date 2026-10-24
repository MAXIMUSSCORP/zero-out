import json

from zeroout import db, tree
from zeroout.ask import Answer, ask
from zeroout.models import Observation, Option

YES = lambda a, b: True
NO = lambda a, b: False
NOT_MAPPED = "Not mapped yet"
UNSURE = "I'm not sure. Here are the top-level options."


class FakeChat:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def __call__(self, settings, model, messages):
        self.calls.append((settings, model, messages))
        if self.exc:
            raise self.exc
        return self.result


class S:
    model_super = "super-model"


def setup():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550001")
    rid = db.start_run(conn, cid, "crawl", "t")
    tree.ensure_root(conn, cid)
    return conn, cid, rid


def menu(text="Press 1 or 2", keys=("1", "2")):
    return Observation(text, [Option(k, f"opt{k}") for k in keys], "menu", 3.0)


def m(conn, cid, rid, path, obs, same=YES):
    return tree.merge(conn, cid, path, obs, rid, "/a.wav", same)


def mapped():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", Observation("Billing", [], "human", 2.0))
    return conn, cid, rid


ROOT_OPTS = [Option("1", "opt1"), Option("2", "opt2")]


def test_valid_path_returned():
    conn, cid, _ = mapped()
    chat = FakeChat({"path": "1", "explanation": "Press 1 for billing"})
    a = ask(conn, S(), cid, "billing?", chat=chat)
    assert isinstance(a, Answer)
    assert a.path == "1" and a.stale is False
    assert a.explanation == "Press 1 for billing"
    assert a.last_confirmed == db.get_node(conn, cid, "1")["last_confirmed"]
    assert a.top_options == []
    assert chat.calls[0][1] == "super-model"


def test_invented_path_rejected():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "9.9", "explanation": "x"}))
    assert a == Answer(None, UNSURE, None, False, ROOT_OPTS)


def test_stale_flagged():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu("Sub", ("3",)))
    m(conn, cid, rid, "", menu("Totally different"), NO)
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "e"}))
    assert db.get_node(conn, cid, "1")["status"] == "stale"
    assert a.path == "1" and a.stale is True


def test_stale_via_stale_ancestor():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu("Sub", ("3",)))
    m(conn, cid, rid, "1.3", Observation("Please hold", [], "hold", 1.0))
    m(conn, cid, rid, "", menu("Rewritten"), NO)
    assert db.get_node(conn, cid, "1")["status"] == "stale"
    m(conn, cid, rid, "1.3", Observation("Please hold", [], "hold", 1.0))
    assert db.get_node(conn, cid, "1.3")["status"] == "mapped"
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1.3", "explanation": "e"}))
    assert a.path == "1.3" and a.stale is True


def test_changed_node_is_fresh():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "1", menu("Sub", ("3",)))
    m(conn, cid, rid, "1", menu("Rewritten", ("3",)), NO)
    assert db.get_node(conn, cid, "1")["status"] == "changed"
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "e"}))
    assert a.path == "1" and a.stale is False


def test_unmapped_company_skips_model():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550001")
    chat = FakeChat({"path": "1"})
    a = ask(conn, S(), cid, "q", chat=chat)
    assert a == Answer(None, NOT_MAPPED, None, False, [])
    assert chat.calls == []


def test_unexplored_root_skips_model():
    conn, cid, _ = setup()
    chat = FakeChat({"path": "1"})
    a = ask(conn, S(), cid, "q", chat=chat)
    assert a.explanation == NOT_MAPPED and chat.calls == []


def test_path_normalised():
    conn, cid, _ = mapped()
    for raw in ("1", "1.", " 1 ", "key 1"):
        a = ask(conn, S(), cid, "q", chat=FakeChat({"path": raw, "explanation": "e"}))
        assert a.path == "1", raw


def test_arrow_path_normalised():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu())
    m(conn, cid, rid, "2", menu("Sub", ("1",)))
    m(conn, cid, rid, "2.1", Observation("Agent", [], "human", 1.0))
    for raw in ("2 → 1", "2,1", "2.1"):
        a = ask(conn, S(), cid, "q", chat=FakeChat({"path": raw, "explanation": "e"}))
        assert a.path == "2.1", raw


def test_unexplored_node_rejected():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "2", "explanation": "e"}))
    assert a.path is None and a.top_options == ROOT_OPTS


def test_bad_responses_rejected():
    conn, cid, _ = mapped()
    for bad in (None, [], "1", {"path": None}, {"path": ""}, {"path": 1}, {}):
        a = ask(conn, S(), cid, "q", chat=FakeChat(bad))
        assert a == Answer(None, UNSURE, None, False, ROOT_OPTS), bad


def test_chat_raises_rejected():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat(exc=RuntimeError("boom")))
    assert a == Answer(None, UNSURE, None, False, ROOT_OPTS)


def test_non_string_explanation_falls_back():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": 5}))
    assert a.path == "1" and a.explanation == ""


def test_question_truncated_and_tree_sent():
    conn, cid, _ = mapped()
    chat = FakeChat({"path": "1", "explanation": "e"})
    ask(conn, S(), cid, "a" * 400 + "b" * 400, chat=chat)
    msgs = chat.calls[0][2]
    assert msgs[0]["role"] == "system"
    user = msgs[-1]["content"]
    assert "a" * 400 + "b" * 100 in user and "b" * 101 not in user
    assert json.dumps(tree.tree_json(conn, cid)) in user


def test_tree_is_fenced_and_labels_flagged_untrusted():
    conn, cid, _ = mapped()
    chat = FakeChat({"path": "1", "explanation": "e"})
    ask(conn, S(), cid, "q", chat=chat)
    msgs = chat.calls[0][2]
    body = msgs[1]["content"]
    assert "<tree>" in body and body.index("<tree>") < body.index("opt1") < body.index("</tree>")
    system = msgs[0]["content"].lower()
    assert "<tree>" in system and "untrusted" in system and "label" in system and "prompt text" in system


def test_explanation_strips_urls_and_phone_numbers():
    conn, cid, _ = mapped()
    text = "Press 1, then visit https://evil.example/x?a=1 or call +1 (900) 555-0100 now"
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": text}))
    assert "http" not in a.explanation and "555" not in a.explanation
    assert a.explanation == "Press 1, then visit or call now"


def test_explanation_capped_and_whitespace_collapsed():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "a \n\t b " + "x" * 1000}))
    assert len(a.explanation) == 300 and a.explanation.startswith("a b x")


def test_short_numbers_like_keys_survive_sanitising():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "Press 1 then 2"}))
    assert a.explanation == "Press 1 then 2"
