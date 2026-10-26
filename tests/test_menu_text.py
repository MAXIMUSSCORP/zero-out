import importlib.util
import random
import string
from pathlib import Path

import pytest

from zeroout.redact import redact
from zeroout.menu_text import menu_lines


def texts(r):
    return [x["text"] for x in r]


def keys(r):
    return [x["key"] for x in r]


def test_blank_gives_no_lines():
    assert menu_lines("") == [] and menu_lines("   \n\t ") == []


def test_no_sentence_marks_is_one_line_and_whitespace_is_normalized():
    r = menu_lines("  thanks   for calling\n  acme wireless ")
    assert r == [{"text": "thanks for calling acme wireless", "key": None}]


def test_sentences_split_keeping_punctuation():
    r = menu_lines("Thanks for calling Acme Wireless. How can we help? Please listen closely!")
    assert texts(r) == ["Thanks for calling Acme Wireless.", "How can we help?", "Please listen closely!"]
    assert keys(r) == [None, None, None]


def test_final_sentence_without_mark_is_kept():
    assert texts(menu_lines("Welcome. For billing press 1")) == ["Welcome.", "For billing press 1"]


def test_never_splits_inside_a_number():
    r = menu_lines("Rates start at 1.5 cents per minute. Press 1 to hear more.")
    assert texts(r) == ["Rates start at 1.5 cents per minute.", "Press 1 to hear more."]


@pytest.mark.parametrize("raw", [
    "Call 415 555 1234 or email help@acme.com. Press 1.",
    "Our number is 8 8 8 5 5 5 0 1 9 9. For billing press 1.",
])
def test_real_redacted_text_keeps_marks_whole(raw):
    red = redact(raw)
    assert "[" in red
    out = menu_lines(red)
    assert " ".join(texts(out)) == red
    assert "[number]" in " ".join(texts(out)) or "[email]" in " ".join(texts(out))
    for t in texts(out):
        assert t.count("[") == t.count("]")


def test_redaction_mark_before_period():
    red = redact("Call 415 555 1234. Press 1 for sales.")
    assert texts(menu_lines(red)) == ["Call [number].", "Press 1 for sales."]


def test_two_options_in_one_sentence_split_at_comma():
    r = menu_lines("For billing press 1, for support press 2.")
    assert texts(r) == ["For billing press 1", "for support press 2."]
    assert keys(r) == ["1", "2"]


def test_split_at_semicolon_and_or():
    assert keys(menu_lines("For sales press 1; for service press 2.")) == ["1", "2"]
    r = menu_lines("For sales press 1 or for service press 2.")
    assert keys(r) == ["1", "2"] and texts(r)[1] == "for service press 2."
    r = menu_lines("For sales press 1, or for service press 2, or for parts press 3.")
    assert keys(r) == ["1", "2", "3"]


def test_comma_inside_a_clause_is_not_the_split():
    r = menu_lines("For billing press 1, for sales or service, press 2.")
    assert keys(r) == ["1", "2"]
    assert texts(r)[1] == "for sales or service, press 2."


def test_single_key_phrase_stays_one_line():
    r = menu_lines("Press 1 for billing, or stay on the line.")
    assert len(r) == 1 and r[0]["key"] == "1"


@pytest.mark.parametrize("said,key", [
    ("press 1", "1"), ("Push 2", "2"), ("dial 3", "3"), ("select 4", "4"), ("enter 5", "5"),
    ("say or press 6", "6"), ("PRESS 7", "7"), ("press star", "*"), ("press *", "*"),
    ("press pound", "#"), ("press #", "#"), ("press the pound key", "#"),
    ("press the star key", "*"), ("press 0", "0"), ("press zero", "0"), ("press nine", "9"),
])
def test_english_key_phrases(said, key):
    assert menu_lines(f"For the operator {said} now.")[0]["key"] == key


@pytest.mark.parametrize("said,key", [
    ("oprima uno", "1"), ("Oprima dos", "2"), ("marque tres", "3"), ("presione cuatro", "4"),
    ("oprima cinco", "5"), ("oprima seis", "6"), ("oprima siete", "7"), ("marque ocho", "8"),
    ("presione nueve", "9"), ("oprima cero", "0"), ("oprima asterisco", "*"),
    ("oprima numeral", "#"), ("presione gato", "#"),
])
def test_spanish_key_phrases(said, key):
    assert menu_lines(f"Para algo {said}.")[0]["key"] == key


def test_acme_wireless_menu():
    r = menu_lines("Thanks for calling Acme Wireless. For billing press 1, for support press 2. "
                   "Para español, oprima dos.")
    assert texts(r) == ["Thanks for calling Acme Wireless.", "For billing press 1", "for support press 2.",
                        "Para español, oprima dos."]
    assert keys(r) == [None, "1", "2", "2"]


def test_a_key_without_a_digit_is_not_a_key():
    r = menu_lines("Please press the button. Enter your account number. Press 12 times.")
    assert keys(r) == [None, None, None]


def test_keys_argument_limits_which_keys_count():
    r = menu_lines("For billing press 1, for support press 2.", keys=["2"])
    assert keys(r) == [None, "2"]
    assert keys(menu_lines("Press 3 now.", keys=["1", "2"])) == [None]
    assert keys(menu_lines("Press 1 now.", keys=iter(["1"]))) == ["1"]


def test_empty_keys_means_all_count():
    assert keys(menu_lines("Press 3 now.", keys=[])) == ["3"]


ODD = [
    "", " ", ".", "...", "?!", "1.", ".5", "[", "]", "[number", "number]", "[number].[email]?",
    "press", "press ", "press *", "press #.", ",;", " or ", "press 1 or press 2 or press 3",
    "press 1,press 2", "oprima", "\x00\x01", "​‮", "😀. 😀? press 1 😀",
    "a" * 50000, ". " * 5000, "press 1, " * 3000, "ñandú. Ünï", "\n\n\t",
]


@pytest.mark.parametrize("s", ODD)
def test_never_raises_on_odd_input(s):
    r = menu_lines(s, keys=["1"])
    assert isinstance(r, list)
    for x in r:
        assert set(x) == {"text", "key"} and x["text"].strip() == x["text"] and x["text"]


def test_never_raises_on_random_strings():
    rng = random.Random(7)
    alphabet = string.printable + "ñé[]*#😀​"
    words = ["press", "oprima", "1", "star", "pound", ".", "?", ",", ";", " or ", "[number]", "dos", "*"]
    for _ in range(400):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        t = " ".join(rng.choice(words) for _ in range(rng.randint(0, 30)))
        for x in (s, t):
            r = menu_lines(x, keys=rng.choice([[], ["1"], ("2", "*")]))
            assert all(isinstance(y["text"], str) and y["key"] in (None, *"0123456789*#") for y in r)


def test_no_words_are_lost_or_invented():
    t = "Thanks. For billing press 1, for support press 2; for the operator press star. Rates 1.5 apply!"
    out = " ".join(texts(menu_lines(t)))
    strip = lambda s: "".join(c for c in s if c.isalnum() or c in "*#")
    assert strip(out) == strip(t)


def test_module_is_stdlib_only_and_loads_by_path():
    path = Path(__file__).parent.parent / "zeroout" / "menu_text.py"
    spec = importlib.util.spec_from_file_location("_menu_text_sibling", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.menu_lines("Press 1.") == [{"text": "Press 1.", "key": "1"}]
    src = path.read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["import re", "from collections.abc import Iterable"]


@pytest.mark.parametrize("s", [
    "." * 20000 + "x", "?!" * 10000 + "x", "." * 20000 + '"' * 20000 + "x", "." * 20000 + ")" * 20000 + "a",
    "press " * 20000, "press 1, " * 20000, ", " * 20000 + "press 1", " or " * 20000, "press the " * 10000,
], ids=range(9))
def test_long_pathological_runs_finish_fast(s):
    import time
    t = time.perf_counter()
    menu_lines(s, keys=["1"])
    assert time.perf_counter() - t < 0.5


def test_punctuation_run_is_one_sentence_end():
    assert texts(menu_lines("Wait... Press 1?! Ok")) == ["Wait...", "Press 1?!", "Ok"]


@pytest.mark.parametrize("s", [
    "Press \u017fix for help.", "Press \u017feven now", "Press \u017ftar", "Press do\u017f", "oprima do\u017f",
    "Press \u212a", "Press \u212aey", "\u212anown: press 1", "PRESS \u017fIX", "oprima \u017f",
])
def test_unicode_case_folding_lookalikes_never_raise_and_are_not_keys(s):
    r = menu_lines(s)
    assert isinstance(r, list) and all(x["key"] in (None, "1") for x in r)
    assert menu_lines("Press \u017fix")[0]["key"] is None


@pytest.mark.parametrize("s", [
    "Please select one of the following options.", "Please enter one of the following",
    "Select ONE of these.", "Oprima uno de los siguientes.", "marque dos de las opciones",
])
def test_a_word_followed_by_of_is_not_a_key(s):
    assert all(x["key"] is None for x in menu_lines(s))


def test_one_alone_is_still_a_key():
    assert keys(menu_lines("Press one for sales. Oprima uno para ventas.")) == ["1", "1"]
    assert keys(menu_lines("Press 1 of 2 times.")) == ["1"]


REPLAYS = [
    "Return to the previous menu", "To repeat this information", "To hear these options again",
    "repeat options", "Repeat this menu", "hear the menu again", "Say that again", "go back",
    "Go back to the previous menu", "return to the main menu", "Return to main", "Start over",
    "the previous menu", "Main menu", "REPEAT THIS MESSAGE", "repetir", "Para repetir esta informacion",
    "volver", "Volver al menú anterior", "MENÚ PRINCIPAL", "MENU ANTERIOR", "VOLVER AL MENÚ", "Para volver a escuchar",
    "Para volver a oír el menú", "Para repetir las opciones", "Para repetir este menú", "Para repetir el menú", "PARA REPETIR EL MENÚ", "Para repetir el menu", "PARA REPETIR ESTA INFORMACIÓN", "Para repetir el mensaje", "menu principal", "menú principal", "To repeat",
]
NOT_REPLAYS = [
    "Repairs", "Return a product", "Returns and exchanges", "Main street store hours", "billing questions",
    "report a lost or stolen phone", "Pharmacy", "Repeat customers desk is closed", "", "   ",
    "Para volver a surtir su receta", "Para volver a programar su cita", "Para volver a enviar su factura",
    "Para repetir su pedido", "Mainframe support", "Go backstage", "Starting overtime pay",
]


@pytest.mark.parametrize("label", REPLAYS)
def test_replays_menu_recognizes_navigation_by_its_label(label):
    from zeroout.menu_text import replays_menu
    assert replays_menu(label) is True


@pytest.mark.parametrize("label", NOT_REPLAYS)
def test_replays_menu_leaves_real_choices_alone(label):
    from zeroout.menu_text import replays_menu
    assert replays_menu(label) is False


@pytest.mark.parametrize("bad", [None, 5, b"repeat", ["repeat"], "\ud800 repeat"])
def test_replays_menu_never_raises(bad):
    from zeroout.menu_text import replays_menu
    assert replays_menu(bad) in (True, False)


def test_replays_menu_ignores_the_key():
    from zeroout.menu_text import replays_menu
    assert replays_menu("sales") is False and replays_menu("*") is False


COSTCO = ("Thank you for calling Costco. For billing questions, press 1. To report a lost or stolen card, press 2. "
          "Para Espanol, oprima uno. For Costco Travel, press 5. Say or press 6 for the pharmacy. "
          "To repeat this information, press star.")


@pytest.mark.parametrize("label,key,want", [
    ("", "5", "Costco Travel"),
    ("one", "1", "billing questions"),
    ("1", "1", "billing questions"),
    ("two", "2", "report a lost or stolen card"),
    ("2", "2", "report a lost or stolen card"),
    ("press 1", "1", "billing questions"),
    ("Press 1", "1", "billing questions"),
    ("Press one", "1", "billing questions"),
    ("oprima uno", "1", "billing questions"),
    ("star", "*", "repeat this information"),
    ("asterisco", "*", "repeat this information"),
    ("pound", "#", "Option #"),
    ("", "6", "the pharmacy"),
    ("", "7", "Option 7"),
    ("press 9", "9", "Option 9"),
    ("  ", "5", "Costco Travel"),
])
def test_fill_label_derives_a_label_from_the_sentence_for_that_key(label, key, want):
    from zeroout.menu_text import fill_label
    assert fill_label(label, key, COSTCO) == want


@pytest.mark.parametrize("label", ["billing", "Costco Travel", "press 1 for billing", "Return a product", "1 hour photo"])
def test_fill_label_keeps_a_real_label(label):
    from zeroout.menu_text import fill_label
    assert fill_label(label, "1", COSTCO) == label


@pytest.mark.parametrize("args", [(None, "1", None), (5, 1, 7), ("", None, "x"), ("one", "1", ""), ("", "1", b"x")])
def test_fill_label_never_raises(args):
    from zeroout.menu_text import fill_label
    assert isinstance(fill_label(*args), str)


def test_fill_label_with_no_prompt_text_names_the_key():
    from zeroout.menu_text import fill_label
    assert fill_label("", "3", "") == "Option 3" and fill_label("three", "3", None) == "Option 3"


@pytest.mark.parametrize("prompt,key,want", [
    ("Press 1 for billing and press 2 to repeat this menu.", "1", "billing"),
    ("Press 1 for billing and press 2 to repeat this menu.", "2", "repeat this menu"),
    ("For billing press 1 to go back press 2.", "1", "billing"),
    ("For billing press 1 to go back press 2.", "2", "go back"),
    ("For sales press 1 for the main menu press 2.", "1", "sales"),
    ("For sales press 1 for the main menu press 2.", "2", "the main menu"),
    ("Press 1 for sales, press 2 for support, or press 3 for billing.", "2", "support"),
    ("Press 1 for sales, press 2 for support, or press 3 for billing.", "3", "billing"),
    ("For sales, press 1, for support, press 2.", "2", "support"),
    ("Say or press 6 for the pharmacy.", "6", "the pharmacy"),
])
def test_fill_label_takes_only_the_words_that_belong_to_that_key(prompt, key, want):
    from zeroout.menu_text import fill_label
    assert fill_label("", key, prompt) == want


@pytest.mark.parametrize("prompt,key", [
    ("Press 1 press 2.", "1"), ("For press 1.", "1"), ("Press 1 and press 2 for sales.", "1"),
])
def test_fill_label_never_swallows_a_neighbouring_option(prompt, key):
    from zeroout.menu_text import fill_label
    got = fill_label("", key, prompt)
    assert got == f"Option {key}" or ("press" not in got.lower() and got)


def test_a_derived_label_never_turns_a_real_option_into_a_replay():
    from zeroout.menu_text import fill_label, replays_menu
    assert not replays_menu(fill_label("", "1", "Press 1 for billing and press 2 to repeat this menu."))
    assert not replays_menu(fill_label("", "1", "For billing press 1 to go back press 2."))
    assert replays_menu(fill_label("", "2", "For billing press 1 to go back press 2."))
