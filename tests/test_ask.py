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


def test_unexplored_option_the_menu_offered_is_answered_with_a_note():
    conn, cid, _ = mapped()  # the root offers 2, and nobody has pressed it yet
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "2", "explanation": "e"}))
    assert a.path == "2" and a.unheard_end is True and a.top_options == []
    assert a.last_confirmed == db.get_node(conn, cid, "")["last_confirmed"]


def costco():
    """The user's real shape: 2 is mapped and offers 3; what plays after pressing 3 is unheard."""
    conn, cid, rid = setup()
    m(conn, cid, rid, "", menu("For members press 2", keys=("1", "2")))
    m(conn, cid, rid, "2", menu("For payment options press 3", keys=("3", "4")))
    return conn, cid, rid


def test_costco_option_heard_offered_but_not_pressed_is_answered():
    conn, cid, _ = costco()
    assert db.get_node(conn, cid, "2.3")["status"] == "unexplored"
    a = ask(conn, S(), cid, "payment?", chat=FakeChat({"path": "2.3", "explanation": "Press 2 then 3"}))
    assert a.path == "2.3" and a.explanation == "Press 2 then 3" and a.unheard_end is True
    assert a.last_confirmed == db.get_node(conn, cid, "2")["last_confirmed"] and a.stale is False


def test_unexplored_option_the_parent_never_offered_is_unsure():
    conn, cid, _ = costco()
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '2.7', 'unexplored')", (cid,))
    conn.commit()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "2.7", "explanation": "e"}))
    assert a.path is None and a.explanation == UNSURE and a.unheard_end is False


def test_unexplored_option_under_an_unheard_parent_is_unsure():
    conn, cid, _ = costco()
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '2.3.1', 'unexplored')", (cid,))
    conn.commit()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "2.3.1", "explanation": "e"}))
    assert a.path is None and a.explanation == UNSURE


def test_fully_mapped_path_has_no_unheard_note():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "e"}))
    assert a.path == "1" and a.unheard_end is False


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


def test_unexplored_child_of_a_terminal_parent_is_unsure_even_if_options_are_stored():
    conn, cid, _ = mapped()  # node 1 is a human; give it stored options and a child row anyway
    conn.execute("UPDATE nodes SET options_json = ? WHERE path = '1'", ('[{"key": "4", "label": ""}]',))
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '1.4', 'unexplored')", (cid,))
    conn.commit()
    assert db.get_node(conn, cid, "1")["outcome"] != "menu"
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1.4", "explanation": "e"}))
    assert a.path is None and a.explanation == UNSURE and a.unheard_end is False


def sent_tree(chat):
    user = chat.calls[0][2][1]["content"]
    return json.loads(user.split("<tree>\n", 1)[1].split("\n</tree>", 1)[0])


def test_human_path_for_talk_to_a_person_gives_keys():
    conn, cid, _ = mapped()
    chat = FakeChat({"path": "1", "explanation": "Press 1 to reach a person", "match": "exact"})
    a = ask(conn, S(), cid, "How do I talk to a person", chat=chat)
    assert a.path == "1" and a.closest is False and a.explanation == "Press 1 to reach a person"


def test_closest_match_sets_the_flag():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "1", "explanation": "Nearest fit", "match": "closest"}))
    assert a.path == "1" and a.closest is True


def test_missing_or_odd_match_is_not_closest():
    conn, cid, _ = mapped()
    for match in (None, "exact", "CLOSEST?", 1, ["closest"]):
        out = {"path": "1", "explanation": "e"}
        if match is not None:
            out["match"] = match
        assert ask(conn, S(), cid, "q", chat=FakeChat(out)).closest is False


def test_closest_with_an_invented_path_is_unsure_and_not_closest():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "9.9", "explanation": "x", "match": "closest"}))
    assert a == Answer(None, UNSURE, None, False, ROOT_OPTS) and a.closest is False


def test_null_path_without_explanation_is_the_generic_unsure():
    conn, cid, _ = mapped()
    for out in ({"path": None}, {"path": None, "explanation": ""}, {"path": None, "explanation": 5}):
        a = ask(conn, S(), cid, "q", chat=FakeChat(out))
        assert a == Answer(None, UNSURE, None, False, ROOT_OPTS)


def test_null_path_shows_the_models_cleaned_explanation_with_top_options():
    conn, cid, _ = mapped()
    said = "Zero-Out hasn't heard an option on this map that reaches a person yet. Call +1 800 555 0100 or see https://x.example"
    a = ask(conn, S(), cid, "talk to a person", chat=FakeChat({"path": None, "explanation": said, "match": "closest"}))
    assert a.path is None and a.closest is False and a.top_options == ROOT_OPTS
    assert a.explanation == "Zero-Out hasn't heard an option on this map that reaches a person yet. Call or see"
    long = ask(conn, S(), cid, "q", chat=FakeChat({"path": None, "explanation": "x" * 1000}))
    assert len(long.explanation) == 300


def test_invented_path_keeps_the_generic_sentence_even_with_an_explanation():
    conn, cid, _ = mapped()
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": "9.9", "explanation": "Press 9.9 for a person"}))
    assert a.explanation == UNSURE


def replay_tree():
    conn, cid, rid = setup()
    m(conn, cid, rid, "", Observation("Press 1 or star", [Option("1", "Billing"), Option("*", "Repeat this information")], "menu", 3.0))
    m(conn, cid, rid, "1", menu("Billing menu", ("3",)))
    m(conn, cid, rid, "*", menu("Again", ("3", "4")))
    m(conn, cid, rid, "*.3", menu("Deeper", ("4",)))
    m(conn, cid, rid, "*.3.4", Observation("Membership", [], "human", 2.0))
    return conn, cid, rid


def test_path_through_a_replay_option_is_unsure_at_any_depth():
    conn, cid, _ = replay_tree()
    for path in ("*", "*.3", "*.3.4"):
        a = ask(conn, S(), cid, "talk to a person", chat=FakeChat({"path": path, "explanation": "x", "match": "closest"}))
        assert a.path is None and a.explanation == UNSURE and a.closest is False
        assert [o.key for o in a.top_options] == ["1", "*"]


def test_model_never_sees_replay_nodes_or_their_subtrees_and_the_stored_tree_is_untouched():
    conn, cid, _ = replay_tree()
    before = tree.tree_json(conn, cid)
    chat = FakeChat({"path": "1", "explanation": "Billing", "match": "exact"})
    assert ask(conn, S(), cid, "bill", chat=chat).path == "1"
    sent = sent_tree(chat)
    assert [c["path"] for c in sent["children"]] == ["1"]
    assert [o["key"] for o in sent["options"]] == ["1"]
    raw = chat.calls[0][2][1]["content"]
    assert "Repeat this information" not in raw and "Deeper" not in raw and "Membership" not in raw
    assert tree.tree_json(conn, cid) == before
    assert [c["path"] for c in before["children"]] == ["*", "1"]
    assert "ignore it and every path through it" in chat.calls[0][2][0]["content"]


def test_a_leading_dot_or_noise_in_the_path_is_normalized():
    conn, cid, _ = mapped()
    for raw in (".1", "1.", " 1 ", "1 ", "key 1"):
        a = ask(conn, S(), cid, "q", chat=FakeChat({"path": raw, "explanation": "e"}))
        assert a.path == "1", raw


def test_prompt_rules_for_person_requests_and_strict_closest():
    from zeroout.ask import SYSTEM
    low = SYSTEM.lower()
    assert "no node has outcome or status human" in low and "return null" in low
    assert "car insurance" in low and "travel insurance" in low and "website inquiries" in low
    assert "same topic and the same product" in low and "ignore it and every path through it" in low


def test_model_sees_statuses_outcomes_and_labels_but_no_private_fields():
    conn, cid, _ = mapped()
    chat = FakeChat({"path": "1", "explanation": "e", "match": "exact"})
    ask(conn, S(), cid, "talk to a person", chat=chat)
    sent = chat.calls[0][2][1]["content"]
    root = sent_tree(chat)
    assert root["status"] and root["outcome"] == "menu"
    assert [o["label"] for o in root["options"]] == ["opt1", "opt2"]
    kid = root["children"][0]
    assert kid["path"] == "1" and kid["outcome"] == "human" and kid["status"]
    for private in ("audio", "wav", "raw_json", "/a.wav", "audio_path"):
        assert private not in sent


def test_prompt_covers_people_closest_and_untrusted_data():
    from zeroout.ask import SYSTEM
    low = SYSTEM.lower()
    for word in ("representative", "agent", "operator", "customer service", "all other questions", "human",
                 "closest", "exact", "never invent", "untrusted", "null", '"match"'):
        assert word in low


def test_ask_with_the_real_chat_sends_temperature_zero(monkeypatch):
    import openai
    from types import SimpleNamespace
    from zeroout.llm import chat_json
    calls = []

    class Client:
        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            calls.append(kw)
            msg = SimpleNamespace(content='{"path": "1", "explanation": "Billing", "match": "exact"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(openai, "OpenAI", Client)
    conn, cid, _ = mapped()
    s = S()
    s.tf_base_url, s.tf_api_key = "http://x", "k"
    a = ask(conn, s, cid, "billing?", chat=chat_json)
    assert a.path == "1" and calls[0]["temperature"] == 0


def test_chat_json_temperature_is_optional_and_passed_through():
    from tests.test_listen import FakeClient, MSGS, make_settings
    from zeroout.llm import chat_json
    c = FakeClient(['{"a": 1}', '{"a": 1}'])
    chat_json(make_settings(), "m", MSGS, client=c)
    chat_json(make_settings(), "m", MSGS, client=c, temperature=0)
    assert "temperature" not in c.calls[0] and c.calls[1]["temperature"] == 0


def test_unsure_answer_lists_top_options_with_derived_labels():
    conn, cid, rid = setup()
    obs = Observation("For billing questions, press 1. For Costco Travel, press 5.",
                      [Option("1", "press 1"), Option("5", "")], "menu", 3.0)
    m(conn, cid, rid, "", obs)
    a = ask(conn, S(), cid, "q", chat=FakeChat({"path": None, "explanation": ""}))
    assert [(o.key, o.label) for o in a.top_options] == [("1", "billing questions"), ("5", "Costco Travel")]
