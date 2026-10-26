import pytest

from zeroout import db, phone, tree
from zeroout.models import Observation, Option

YES = lambda a, b: True
NO = lambda a, b: False
UNSURE = lambda a, b: None


def setup():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550001")
    tree.ensure_root(conn, cid)
    rid = phone.start_phone_run(conn, cid)
    return conn, cid, rid


def menu(keys=("1", "2"), text="Press a key"):
    return Observation(text, [Option(k, f"opt{k}") for k in keys], "menu", 3.0)


def hold():
    return Observation("", [], "hold", 1.0)


def seed(conn, cid, rid, path, obs):
    tree.merge(conn, cid, path, obs, rid, "", YES)


def keep(conn, cid, rid, path, obs, same=YES):
    return phone.keep_clip(conn, cid, path, obs, run_id=rid, same_meaning=same)


def hear(obs, recognized=True, **kw):
    kw.setdefault("capped", False)
    kw.setdefault("sound", False)
    return phone.hear_clip(None, b"RIFF", hear=lambda s, w: (recognized, obs), **kw)


# ---------- next_step ----------

def test_fresh_company_starts_at_the_top_menu():
    conn, cid, rid = setup()
    assert phone.next_step(conn, cid, None) == phone.Step("", [], None, "")


def test_company_with_no_nodes_starts_at_the_top_menu():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Fresh", "+18005550009")
    assert phone.next_step(conn, cid, None) == phone.Step("", [], None, "")
    assert db.get_node(conn, cid, "") is None  # asking does not write


def test_children_of_current_come_first_in_keypad_order():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("0", "#", "*", "2", "1")))
    assert phone.next_step(conn, cid, "") == phone.Step("1", ["1"], "1", "opt1")
    seed(conn, cid, rid, "1", menu(("1",)))
    seed(conn, cid, rid, "2", menu(("1",)))
    assert phone.next_step(conn, cid, "2") == phone.Step("2.1", ["2", "1"], "1", "opt1")


def test_keypad_order_puts_zero_after_nine_then_star_hash():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("#", "*", "0", "9")))
    seen = []
    for _ in range(4):
        step = phone.next_step(conn, cid, "")
        seen.append(step.path)
        seed(conn, cid, rid, step.path, hold())
    assert seen == ["9", "0", "*", "#"]


def test_falls_back_to_shallowest_elsewhere_without_press_now():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2")))
    seed(conn, cid, rid, "1", menu(("1", "2")))
    seed(conn, cid, rid, "1.1", hold())
    seed(conn, cid, rid, "1.2", hold())
    assert phone.next_step(conn, cid, "1.2") == phone.Step("2", ["2"], None, "opt2")


def test_shallowest_then_keypad_order_along_the_path():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2")))
    seed(conn, cid, rid, "1", menu(("2", "1")))
    seed(conn, cid, rid, "2", menu(("1",)))
    seed(conn, cid, rid, "1.1", menu(("1",)))
    assert phone.next_step(conn, cid, None).path == "1.2"
    seed(conn, cid, rid, "1.2", hold())
    assert phone.next_step(conn, cid, None).path == "2.1"


def test_stale_nodes_count_as_candidates():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1",)))
    seed(conn, cid, rid, "1", menu(("1",)))
    seed(conn, cid, rid, "1.1", hold())
    assert phone.next_step(conn, cid, None) is None
    conn.execute("UPDATE nodes SET status='stale' WHERE path='1'")
    assert phone.next_step(conn, cid, None).path == "1"


def test_none_when_everything_is_mapped():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1",)))
    seed(conn, cid, rid, "1", hold())
    assert phone.next_step(conn, cid, "1") is None


def test_depth_limit():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1",)))
    seed(conn, cid, rid, "1", menu(("1",)))
    assert phone.next_step(conn, cid, "1", max_depth=1) is None
    assert phone.next_step(conn, cid, "1", max_depth=2).path == "1.1"


def test_label_names_the_key_when_parent_gave_none():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", Observation("x", [Option("5", "")], "menu", 1.0))
    assert phone.next_step(conn, cid, "").label == "Option 5"


def test_step_label_comes_from_the_prompt_when_the_stored_label_is_only_a_key():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", Observation("For Costco Travel, press 5.", [Option("5", "five")], "menu", 1.0))
    assert phone.next_step(conn, cid, "").label == "Costco Travel"


def test_skipped_path_is_passed_over():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2", "3")))
    # the reviewer's case: at the root, 1 is skipped, so 2 comes next and 1 never again
    assert phone.next_step(conn, cid, "", skip=frozenset({"1"})) == phone.Step("2", ["2"], "2", "opt2")
    assert phone.next_step(conn, cid, "", skip={"1", "2"}).path == "3"
    assert phone.next_step(conn, cid, "", skip={"1", "2", "3"}) is None


def test_skip_covers_descendants():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2")))
    seed(conn, cid, rid, "1", menu(("1",)))
    seed(conn, cid, rid, "2", hold())
    assert phone.next_step(conn, cid, "1", skip={"1"}) is None


def test_skipping_the_root_skips_everything():
    conn, cid, rid = setup()
    assert phone.next_step(conn, cid, None, skip={""}) is None
    fresh = db.connect(":memory:")
    other = db.add_company(fresh, "F", "+18005550008")
    assert phone.next_step(fresh, other, None, skip={""}) is None


def test_retry_step_repeats_the_path_without_a_key_to_press():
    assert phone.retry_step("2.1") == phone.Step("2.1", ["2", "1"], None, "")
    assert phone.retry_step("") == phone.Step("", [], None, "")


def test_start_phone_run_row():
    conn, cid, rid = setup()
    row = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    assert (row["kind"], row["contributor_handle"], row["calls"], row["cost_usd"]) == ("phone", "phone", 0, 0)


# ---------- hear_clip ----------

def test_no_speech_plain_is_nothing_usable():
    out = hear(None, recognized=False)
    assert out.obs is None
    assert out.message == "Nothing usable was heard. Put the phone on speaker close to the computer and try again."


def test_no_speech_capped_with_sound_is_hold():
    out = hear(None, recognized=False, capped=True, sound=True)
    assert out.obs == Observation("", [], "hold", 0.0)
    assert out.message == "That path plays hold music or waits without a menu."


def test_no_speech_capped_without_sound_is_hangup():
    out = hear(None, recognized=False, capped=True, sound=False)
    assert out.obs == Observation("", [], "hangup", 0.0)
    assert out.message == "Nothing came through for 90 seconds, so that path seems to end the call."


def test_sound_alone_without_cap_is_not_a_hold():
    assert hear(None, recognized=False, capped=False, sound=True).obs is None


def test_speech_but_no_observation_is_unreadable():
    out = hear(None, recognized=True, capped=True, sound=True)
    assert out.obs is None
    assert out.message == "The menu was heard but could not be read. Try again."


def test_menu_is_heard_with_a_count():
    out = hear(menu(("1",), "For sales press 1"))
    assert out.message == "Heard 1 option."
    assert out.obs.prompt_text == "For sales press 1" and out.obs.options == [Option("1", "opt1")]
    assert hear(menu()).message == "Heard 2 options."


@pytest.mark.parametrize("outcome,text", [
    ("human", phone.MESSAGES["human"]),
    ("voicemail", "That path reached a voicemail greeting, so Zero-Out kept none of its words."),
    ("hold", "That path puts you on hold."),
    ("hangup", "The call ended on that path."),
    ("needs_input", "That path asks for account details, so Zero-Out stops there."),
    ("speech_menu", "That menu wants you to speak instead of pressing keys, so Zero-Out stops there."),
    ("invalid_input", "The menu did not accept that key."),
])
def test_non_menu_outcomes_blank_the_words_and_say_what_happened(outcome, text):
    out = hear(Observation("Hi this is Dana at 780 555 0199", [], outcome, 2.0))
    assert out.obs.outcome == outcome and out.obs.prompt_text == ""
    assert out.message == text


@pytest.mark.parametrize("said,kept", [
    ("account 12345678 please", "account [number] please"),
    ("call 780 555 0199 now", "call [number] now"),
    ("call 780-555-0199 now", "call [number] now"),
    ("call 780.555.0199 now", "call [number] now"),
    ("pin 1 2 3 4", "pin [number]"),
    ("say one two three four now", "say [number] now"),
    ("Say ONE Two three FOUR", "Say [number]"),
    ("uno dos tres cuatro cinco", "[number]"),
    ("code 1 two 3 four", "code [number]"),
    ("write to help@acme-corp.example.com today", "write to [email] today"),
    ("mail a.b+c@acme.ca or 5551234", "mail [email] or [number]"),
])
def test_redaction_of_prompt_text(said, kept):
    assert hear(menu(("1",), said)).obs.prompt_text == kept


def test_redaction_keeps_short_numbers_and_key_words():
    text = "For sales press 1, for billing press 2, open 24 hours, one moment please"
    assert hear(menu(("1",), text)).obs.prompt_text == text


def test_redaction_applies_to_labels_too():
    obs = Observation("x", [Option("1", "call 780 555 0199"), Option("2", "mail a@b.ca")], "menu", 1.0)
    assert [o.label for o in hear(obs).obs.options] == ["call [number]", "mail [email]"]


def test_hear_clip_does_not_change_the_observation_it_was_given():
    original = menu(("1",), "pin 1234")
    hear(original)
    assert original.prompt_text == "pin 1234"


def test_hear_clip_errors_propagate():
    def boom(settings, wav):
        raise RuntimeError("model missing")

    with pytest.raises(RuntimeError, match="model missing"):
        phone.hear_clip(None, b"x", capped=False, sound=False, hear=boom)

    def bad(settings, wav):
        raise ValueError("bad audio")

    with pytest.raises(ValueError, match="bad audio"):
        phone.hear_clip(None, b"x", capped=False, sound=False, hear=bad)


def test_hear_clip_defaults_to_the_real_listener():
    from zeroout.listen import hear_words
    assert phone.hear_clip.__kwdefaults__["hear"] is hear_words


# ---------- keep_clip ----------

def test_menu_clip_merges_and_points_at_first_option():
    conn, cid, rid = setup()
    out = keep(conn, cid, rid, "", menu(("1", "2")))
    assert out.outcome == "menu" and out.result == "mapped"
    assert [o.key for o in out.options] == ["1", "2"]
    assert out.message == "Heard 2 options. This is new, so it is now on the map."
    assert out.next == phone.Step("1", ["1"], "1", "opt1")
    node = db.get_node(conn, cid, "")
    assert (node["status"], node["source"]) == ("mapped", "phone")


def test_one_option_is_singular():
    conn, cid, rid = setup()
    assert keep(conn, cid, rid, "", menu(("1",))).message.startswith("Heard 1 option. ")


def test_menu_confirmed_changed_and_undecided_messages():
    conn, cid, rid = setup()
    keep(conn, cid, rid, "", menu())
    assert keep(conn, cid, rid, "", menu(), YES).message == "Heard 2 options. It matches what was heard before."
    assert keep(conn, cid, rid, "", menu(), UNSURE).message == (
        "Heard 2 options. Zero-Out could not tell whether it matches what was heard before, "
        "so it kept the earlier version.")
    out = keep(conn, cid, rid, "", menu(("1", "3")))
    assert out.result == "changed"
    assert out.message == (
        "Heard 2 options. It differs from what was heard before, so the options below will be checked again.")


@pytest.mark.parametrize("outcome", ["human", "voicemail", "hold", "hangup", "needs_input", "speech_menu"])
def test_terminal_outcome_messages(outcome):
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2")))
    out = keep(conn, cid, rid, "1", Observation("", [], outcome, 2.0))
    assert out.outcome == outcome and out.message == phone.MESSAGES[outcome] and out.options == []
    assert out.next == phone.Step("2", ["2"], None, "opt2")


def test_invalid_input_retries_the_same_path():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1",)))
    out = keep(conn, cid, rid, "1", Observation("", [], "invalid_input", 1.0))
    assert out.result == "retry"
    assert out.message == "The menu did not accept that key."
    assert out.next.path == "1"


def test_phone_clip_that_differs_from_a_crawl_is_disputed():
    conn, cid, rid = setup()
    seed(conn, cid, rid, "", menu(("1", "2")))
    seed(conn, cid, rid, "1", menu(("1",)))
    out = keep(conn, cid, rid, "", menu(("1", "3")))
    assert out.result == "disputed"
    assert out.message == "This differs from what an automatic call heard before, so the earlier version was kept."
    assert db.get_node(conn, cid, "")["source"] == "crawl"
    assert db.get_node(conn, cid, "1")["status"] == "mapped"


def test_no_audio_is_stored_and_run_totals_stay_zero():
    conn, cid, rid = setup()
    keep(conn, cid, rid, "", menu())
    rows = conn.execute("SELECT audio_url, run_id FROM observations").fetchall()
    assert [(r["audio_url"], r["run_id"]) for r in rows] == [(None, rid)]
    run = conn.execute("SELECT calls, minutes, cost_usd FROM runs WHERE id=?", (rid,)).fetchone()
    assert tuple(run) == (0, 0, 0)


def test_top_menu_path_works_on_a_company_without_a_root():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "B", "+18005550002")
    rid = phone.start_phone_run(conn, cid)
    assert keep(conn, cid, rid, "", menu()).result == "mapped"


def test_unknown_path_raises():
    conn, cid, rid = setup()
    with pytest.raises(ValueError, match="That option is not on this company's map."):
        keep(conn, cid, rid, "7.7", menu())
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_path_of_another_company_raises():
    conn, cid, rid = setup()
    other = db.add_company(conn, "C", "+18005550003")
    seed(conn, cid, rid, "", menu())
    with pytest.raises(ValueError):
        keep(conn, other, rid, "1", menu())


def test_labels_are_redacted_on_non_menu_outcomes_too():
    obs = Observation("x", [Option("1", "account 4500 1234"), Option("2", "mail a@b.ca")], "needs_input", 1.0)
    out = hear(obs)
    assert out.obs.prompt_text == ""
    assert [o.label for o in out.obs.options] == ["account [number]", "mail [email]"]


def test_next_step_never_offers_a_repeat_option():
    conn, cid, rid = setup()
    obs = Observation("press", [Option("*", "To repeat this information"), Option("2", "sales")], "menu", 3.0)
    seed(conn, cid, rid, "", obs)
    step = phone.next_step(conn, cid, "")
    assert step.path == "2" and step.press_now == "2"
    seed(conn, cid, rid, "2", hold())
    assert phone.next_step(conn, cid, "2") is None
