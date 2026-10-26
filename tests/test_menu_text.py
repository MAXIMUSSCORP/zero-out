import importlib.util
import random
import string
from pathlib import Path

import pytest

from zeroout.redact import redact
from zeroout.menu_text import menu_lines, spoken_examples


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
    "Repairs", "Return a product", "Returns and exchanges", "Main street store hours", "Billing questions",
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
    ("one", "1", "Billing questions"),
    ("1", "1", "Billing questions"),
    ("two", "2", "Report a lost or stolen card"),
    ("2", "2", "Report a lost or stolen card"),
    ("press 1", "1", "Billing questions"),
    ("Press 1", "1", "Billing questions"),
    ("Press one", "1", "Billing questions"),
    ("oprima uno", "1", "Billing questions"),
    ("star", "*", "Repeat this information"),
    ("asterisco", "*", "Repeat this information"),
    ("pound", "#", "Option #"),
    ("", "6", "The pharmacy"),
    ("", "7", "Option 7"),
    ("press 9", "9", "Option 9"),
    ("  ", "5", "Costco Travel"),
])
def test_fill_label_derives_a_label_from_the_sentence_for_that_key(label, key, want):
    from zeroout.menu_text import fill_label
    assert fill_label(label, key, COSTCO) == want


@pytest.mark.parametrize("label", ["billing", "Costco Travel", "press 1 for billing", "Return a product", "1 hour photo"])
def test_fill_label_keeps_a_real_label_when_the_prompt_has_no_sentence_for_that_key(label):
    from zeroout.menu_text import fill_label
    assert fill_label(label, "8", COSTCO) == label


def test_fill_label_prefers_the_words_of_the_prompt_over_a_stored_label_that_shares_a_word():
    from zeroout.menu_text import fill_label
    assert fill_label("Billing", "1", COSTCO) == "Billing questions"
    assert fill_label("Return a product", "1", COSTCO) == "Return a product"  # unrelated, and no other key's


@pytest.mark.parametrize("args", [(None, "1", None), (5, 1, 7), ("", None, "x"), ("one", "1", ""), ("", "1", b"x")])
def test_fill_label_never_raises(args):
    from zeroout.menu_text import fill_label
    assert isinstance(fill_label(*args), str)


def test_fill_label_with_no_prompt_text_names_the_key():
    from zeroout.menu_text import fill_label
    assert fill_label("", "3", "") == "Option 3" and fill_label("three", "3", None) == "Option 3"


@pytest.mark.parametrize("prompt,key,want", [
    ("Press 1 for billing and press 2 to repeat this menu.", "1", "Billing"),
    ("Press 1 for billing and press 2 to repeat this menu.", "2", "Repeat this menu"),
    ("For billing press 1 to go back press 2.", "1", "Billing"),
    ("For billing press 1 to go back press 2.", "2", "Go back"),
    ("For sales press 1 for the main menu press 2.", "1", "Sales"),
    ("For sales press 1 for the main menu press 2.", "2", "The main menu"),
    ("Press 1 for sales, press 2 for support, or press 3 for billing.", "2", "Support"),
    ("Press 1 for sales, press 2 for support, or press 3 for billing.", "3", "Billing"),
    ("For sales, press 1, for support, press 2.", "2", "Support"),
    ("Say or press 6 for the pharmacy.", "6", "The pharmacy"),
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


UNITED = "You can say things like, I need help changing my reservation, or, I'd like to book a flight."


def test_united_examples():
    assert spoken_examples(UNITED) == ["I need help changing my reservation", "I'd like to book a flight"]


@pytest.mark.parametrize("text,want", [
    ("Say something like check my balance or pay my bill.", ["Check my balance", "Pay my bill"]),
    ("For example, say track my package.", ["Track my package"]),
    ("For example, 'where is my order'.", ["Where is my order"]),
    ("Try saying \"change my flight\"; or \"cancel a trip\".", ["Change my flight", "Cancel a trip"]),
    ("You can say check status, or say talk to an agent.", ["Check status", "Talk to an agent"]),
    ("Welcome to Acme. You can say things like pay my bill. Anything else is fine.", ["Pay my bill"]),
    ("Puede decir cosas como, quiero pagar mi factura, o, necesito ayuda con mi cuenta.",
     ["Quiero pagar mi factura", "Necesito ayuda con mi cuenta"]),
    ("Por ejemplo, diga estado de mi pedido.", ["Estado de mi pedido"]),
    ("You can say \u201cbook a flight\u201d.", ["Book a flight"]),
])
def test_example_phrasings(text, want):
    assert spoken_examples(text) == want


@pytest.mark.parametrize("text", [
    "", "   ", "For billing press 1, for support press 2.", "Thanks for calling. Para espanol, oprima dos.",
    "You can say things like.", "You can say billing.", "You can say call 5551234567 now.",
    "You can say things like [number] or [email].", "Say yes", "Welcome, you can press 1 for sales.",
])
def test_no_examples(text):
    assert spoken_examples(text) == []


def test_examples_drop_digits_and_redaction_marks_but_keep_the_rest():
    assert spoken_examples("You can say call [number] now, or pay my bill, or book flight 2 please.") == ["Pay my bill"]


def test_examples_are_capped_trimmed_and_unique():
    many = "You can say " + "; ".join(f"do thing {c}ne" for c in "abcdefgh")
    assert len(spoken_examples(many)) == 5
    long = spoken_examples("You can say " + "word " * 60)
    assert len(long) == 1 and len(long[0]) <= 80
    assert spoken_examples("You can say pay my bill; pay my bill") == ["Pay my bill"]


@pytest.mark.parametrize("s", ODD + [None, 5, b"x", "You can say " * 3000, "or say " * 5000, "for example, " * 4000,
                                        "You can say \u017fay x", "\u212a"])
def test_examples_never_raise(s):
    r = spoken_examples(s)
    assert isinstance(r, list) and len(r) <= 5 and all(isinstance(x, str) and len(x) <= 80 for x in r)


def test_examples_time_bounded():
    import time
    t = time.perf_counter()
    spoken_examples("You can say " + ", or " * 20000)
    spoken_examples("you can say " + "a " * 50000)
    assert time.perf_counter() - t < 0.5


TRAVEL = ("Thank you for calling Costco Travel. For rental cars, press 1. For cruises, press 2. "
          "For specialty travel or guided vacations. Press 3. For travel within the United States. Press 4. "
          "For Canada vacation packages. Press 5. For all other international travel. Press 6. "
          "For travel insurance or if you are in need of in-travel or post-travel assistance. Press 7. "
          "To repeat these options, press the star key.")


def test_a_bare_press_sentence_joins_the_choice_sentence_before_it():
    lines = menu_lines(TRAVEL)
    assert [x["text"] for x in lines][:5] == [
        "Thank you for calling Costco Travel.", "For rental cars, press 1.", "For cruises, press 2.",
        "For specialty travel or guided vacations, press 3.", "For travel within the United States, press 4."]
    assert [x["key"] for x in lines] == [None, "1", "2", "3", "4", "5", "6", "7", "*"]
    assert lines[7]["text"] == ("For travel insurance or if you are in need of in-travel or post-travel "
                                "assistance, press 7.")
    assert lines[8] == {"text": "To repeat these options, press the star key.", "key": "*"}


@pytest.mark.parametrize("bare,key", [
    ("Press the star key.", "*"), ("Press the pound key.", "#"), ("Press pound.", "#"), ("Oprima tres.", "3"),
    ("Press 3", "3"), ("Say or press 3.", "3"),
])
def test_bare_key_sentences_of_every_shape_join_the_previous_line(bare, key):
    lines = menu_lines(f"For the thing. {bare}")
    assert len(lines) == 1 and lines[0]["key"] == key and lines[0]["text"].startswith("For the thing, ")


def test_a_bare_press_sentence_with_no_choice_before_it_stands_alone():
    assert [x["key"] for x in menu_lines("Press 3.")] == ["3"]
    lines = menu_lines("For billing, press 1. Press 2.")
    assert [x["text"] for x in lines] == ["For billing, press 1.", "Press 2."]


def test_filtered_keys_still_apply_to_a_joined_line():
    assert [x["key"] for x in menu_lines("For cars. Press 3.", ["4"])] == [None]
    assert [x["key"] for x in menu_lines("For cars. Press 3.", ["3"])] == ["3"]


@pytest.mark.parametrize("key,want", [
    ("1", "Rental cars"), ("2", "Cruises"), ("3", "Specialty travel or guided vacations"),
    ("4", "Travel within the United States"), ("5", "Canada vacation packages"),
    ("6", "All other international travel"),
    ("7", "Travel insurance or if you are in need of in-travel or post-travel assistance"),
    ("*", "Repeat these options"),
])
def test_fill_label_prefers_the_transcript_over_shifted_stored_labels(key, want):
    from zeroout.menu_text import fill_label, replays_menu
    shifted = {"1": "rental cars", "2": "cruises", "3": "travel within the United States", "4": "Canada vacation packages",
               "5": "all other international travel", "6": "travel insurance or if", "7": "", "*": "star"}
    assert fill_label(shifted.get(key, ""), key, TRAVEL) == want
    assert replays_menu(fill_label("", key, TRAVEL)) is (key == "*")


RENTAL = ("This menu is for rental car reservations that are not combined with any other hotel reservation. "
          "If you need assistance with a rental car that is part of a package, please press pound to return to "
          "the previous menu and select the destination in which your package is for. "
          "For new bookings or to make changes to your existing reservation, press 1. "
          "For help with your online account or to book on CostcoTravel.com, press 2. "
          "For in travel assistance, press 3. To repeat these options, press the star key. "
          "To return to the previous, press the pound key.")


@pytest.mark.parametrize("key,want", [
    ("1", "New bookings or to make changes to your existing reservation"),
    ("2", "Help with your online account or to book on CostcoTravel.com"),
    ("3", "In travel assistance"),
    ("*", "Repeat these options"),
    ("#", "Return to the previous"),
])
def test_fill_label_skips_a_condition_and_prefers_the_sentence_that_ends_on_the_key(key, want):
    from zeroout.menu_text import fill_label
    assert fill_label("", key, RENTAL) == want


def test_a_condition_before_the_key_is_never_the_label_and_the_words_after_it_are_used():
    from zeroout.menu_text import fill_label, replays_menu
    text = ("If you need assistance with a rental car that is part of a package, please press pound to return "
            "to the previous menu.")
    got = fill_label("", "#", text)
    assert got == "Return to the previous menu" and replays_menu(got)
    assert fill_label("", "1", "If you are a member, please press 1.") == "Option 1"
    assert fill_label("", "1", "When you are ready then press 1.") == "Option 1"


@pytest.mark.parametrize("key", ["1", "2", "3"])
def test_the_rental_menu_choices_are_not_replays(key):
    from zeroout.menu_text import fill_label, replays_menu
    assert not replays_menu(fill_label("", key, RENTAL))


def test_replay_detection_ignores_a_word_after_the_key_in_a_before_style_sentence():
    from zeroout.menu_text import fill_label
    assert fill_label("", "1", "For sales press 1 for the main menu press 2.") == "Sales"


@pytest.mark.parametrize("prompt,key", [
    ("To start over with a new booking, press 2.", "2"),
    ("If you want to go back to a previous order, press 4.", "4"),
    ("For questions about the main menu items on our website, press 3.", "3"),
    ("To go back to your account settings and change your address, press 5.", "5"),
])
def test_a_choice_that_merely_mentions_navigation_is_not_a_replay(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("", key, prompt) is False


@pytest.mark.parametrize("prompt,key,stored", [
    ("To repeat these options, press the star key.", "*", ""),
    ("Please press pound to return to the previous menu.", "#", ""),
    ("To return to the previous, press the pound key.", "#", ""),
    ("To go back, press 9.", "9", ""),
    ("If you would like to go back, press 9.", "9", ""),
    ("For billing press 1.", "*", "Repeat this information"),
    ("For billing press 1.", "8", "return to the previous menu"),  # nothing derivable: the stored label is read
    ("To start over, press 7.", "7", ""),
    ("To hear these options again, press star.", "*", ""),
    ("Para repetir las opciones, oprima asterisco.", "*", ""),
])
def test_navigation_phrases_that_open_a_short_label_are_replays(prompt, key, stored):
    from zeroout.menu_text import option_replays
    assert option_replays(stored, key, prompt) is True


def test_the_fixtures_replay_only_where_they_navigate():
    from zeroout.menu_text import option_replays
    assert [k for k in "1234567*" if option_replays("", k, TRAVEL)] == ["*"]
    assert [k for k in "123*#" if option_replays("", k, RENTAL)] == ["*", "#"]
    # the derived label decides: "#" derives "Return to the previous" even when the stored label is wrong
    assert option_replays("Return to the previous menu", "#", RENTAL) and option_replays("billing", "#", RENTAL)


def test_a_shifted_stored_replay_label_does_not_hide_a_real_option():
    from zeroout.menu_text import option_replays
    assert option_replays("To repeat these options", "2", "For rental cars, press 1. For cruises, press 2.") is False
    assert option_replays("To repeat these options", "7", TRAVEL) is False
    assert option_replays("Start over with a new booking", "2", "To start over with a new booking, press 2.") is False
    assert option_replays("To repeat these options", "*", TRAVEL) is True


def test_the_six_acceptance_cases():
    from zeroout.menu_text import option_replays
    assert option_replays("", "#", RENTAL) is True
    assert option_replays("", "2", "To start over with a new booking, press 2.") is False
    assert option_replays("", "4", "If you want to go back to a previous order, press 4.") is False
    assert option_replays("", "3", "For questions about the main menu items on our website, press 3.") is False
    assert option_replays("To repeat these options", "2", "For cruises, press 2.") is False
    assert option_replays("", "*", TRAVEL) is True


def test_a_long_intro_sentence_does_not_merge_with_a_bare_press_sentence():
    lines = menu_lines("To protect your privacy, calls may be recorded. Press 1.")
    assert [(x["text"], x["key"]) for x in lines] == [
        ("To protect your privacy, calls may be recorded.", None), ("Press 1.", "1")]
    from zeroout.menu_text import fill_label
    assert fill_label("", "1", "To protect your privacy, calls may be recorded. Press 1.") == "Option 1"


def test_a_short_choice_sentence_still_merges_and_si_opens_one():
    assert [(x["text"], x["key"]) for x in menu_lines("Si desea hablar con un agente. Oprima 0.")] == [
        ("Si desea hablar con un agente, oprima 0.", "0")]
    assert menu_lines("For specialty travel or guided vacations. Press 3.")[0]["key"] == "3"


@pytest.mark.parametrize("prompt,key,want", [
    ("Are you calling about a Costco.com order you've already placed, including order status, returning, or "
     "canceling an order? Press 1.", "1",
     "A Costco.com order you've already placed, including order status, returning, or canceling an order"),
    ("Do you need product information or assistance placing an order online? Press 2.", "2",
     "Product information or assistance placing an order online"),
    ("Would you like help with your membership or questions about the executive 2% reward program? Press 4.", "4",
     "Help with your membership or questions about the executive 2% reward program"),
    ("Costco Logistics Delivery Questions? Press 2.", "2", "Costco Logistics Delivery Questions"),
    ("Looking for product information? Press 1.", "1", "Product information"),
    ("Do you have questions about your order? Press 3.", "3", "Questions about your order"),
    ("Is this about a warranty claim? Press 6.", "6", "A warranty claim"),
])
def test_a_question_sentence_and_its_press_sentence_make_one_labelled_line(prompt, key, want):
    from zeroout.menu_text import fill_label
    lines = menu_lines(prompt)
    assert len(lines) == 1 and lines[0]["key"] == key
    assert fill_label("", key, prompt) == want


@pytest.mark.parametrize("prompt,key,want", [
    ("If you wish to speak with a Costco representative, press Zero.", "0", "Speak with a Costco representative"),
    ("If you need assistance while traveling, press 2.", "2", "Need assistance while traveling"),
    ("If you do not have your order number, press 8.", "8", "Do not have your order number"),
    ("If you have questions about a shop card ordered online, press 2.", "2",
     "Questions about a shop card ordered online"),
    ("If you want to hear this again, press Star.", "*", "Hear this again"),
])
def test_an_if_you_clause_with_nothing_after_the_key_is_the_label(prompt, key, want):
    from zeroout.menu_text import fill_label
    assert fill_label("", key, prompt) == want


def test_an_if_clause_ending_in_please_is_still_not_a_label():
    from zeroout.menu_text import fill_label
    assert fill_label("", "1", "If you are a member, please press 1.") == "Option 1"
    assert fill_label("", "#", "If you need assistance, please press pound to return to the previous menu.") \
        == "Return to the previous menu"


@pytest.mark.parametrize("prompt,key,want", [
    ("For Tahiti, Fiji, or the Cook Islands. Press 5.", "5", "Tahiti, Fiji, or the Cook Islands"),
    ("For California, including Disneyland, or Florida, including Disney World. Press 2.", "2",
     "California, including Disneyland, or Florida, including Disney World"),
    ("Para ventas, servicio y soporte. Oprima 3.", "3", "Ventas, servicio y soporte"),
    ("Si desea hablar con un agente, ahora. Oprima 0.", "0", "Desea hablar con un agente, ahora"),
])
def test_for_lines_with_commas_merge_at_any_length(prompt, key, want):
    from zeroout.menu_text import fill_label
    assert len(menu_lines(prompt)) == 1
    assert fill_label("", key, prompt) == want


def test_only_a_to_line_with_a_comma_stays_apart():
    assert len(menu_lines("To protect your privacy, calls may be recorded. Press 1.")) == 2
    assert len(menu_lines("To repeat this information. Press Star.")) == 1


def test_press_zero_and_press_star_in_capitals_are_keys():
    assert [x["key"] for x in menu_lines("Press Zero. Press Star. Press Pound. Oprima CERO.")] == ["0", "*", "#", "0"]


def test_the_costco_replays_hold_after_the_new_merges():
    from zeroout.menu_text import option_replays
    assert option_replays("", "*", "To repeat this information. Press Star.") is True
    assert option_replays("", "2", "If you need assistance while traveling, press 2.") is False
    assert option_replays("", "0", "If you wish to speak with a Costco representative, press Zero.") is False
    assert option_replays("", "#", RENTAL) is True and option_replays("", "*", TRAVEL) is True
    assert option_replays("", "4", "If you want to go back to a previous order, press 4.") is False


@pytest.mark.parametrize("prompt,key", [
    ("To return to the main menu at any time, press star.", "*"),
    ("Press star at any time to return to the main menu.", "*"),
    ("To repeat these options or to hear them again, press star.", "*"),
    ("If you need assistance with a rental car that is part of a package, please press pound to return to the "
     "previous menu and select the destination in which your package is for.", "#"),
    ("At any time, press star to go back.", "*"),
    ("Press pound to return to the previous menu now.", "#"),
    ("To start over at any time, press 9.", "9"),
])
def test_common_replay_phrasings_are_replays(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("", key, prompt) is True


@pytest.mark.parametrize("prompt,key", [
    ("To start over with a new booking, press 2.", "2"),
    ("To start over with a new booking at any time, press 2.", "2"),
    ("If you want to go back to a previous order, press 4.", "4"),
    ("For questions about the main menu items on our website, press 3.", "3"),
    ("To return an item and get a refund, press 5.", "5"),
    ("Press 6 at any time to speak with a representative.", "6"),
    ("To hear them again with a new account, press 7.", "7"),
])
def test_filler_stripping_does_not_turn_choices_into_replays(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("", key, prompt) is False


def test_the_shown_label_keeps_its_filler_while_the_replay_decision_does_not():
    from zeroout.menu_text import fill_label
    assert fill_label("", "*", "To return to the main menu at any time, press star.") == "Return to the main menu at any time"
    assert fill_label("", "*", "Press star at any time to return to the main menu.") == "At any time to return to the main menu"


@pytest.mark.parametrize("prompt,key", [
    ("For the main menu, press 9.", "9"),
    ("For the main menu press 0.", "0"),
    ("For the previous menu, press star.", "*"),
    ("To go to the main menu, press star.", "*"),
    ("To go back to the previous menu, press pound.", "#"),
    ("For sales press 1 for the main menu press 2.", "2"),
    ("To return to the previous menu, press pound.", "#"),
])
def test_for_the_main_menu_and_go_to_forms_are_replays(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("", key, prompt) is True


@pytest.mark.parametrize("prompt,key", [
    ("To start over with a new booking, press 2.", "2"),
    ("For main menu items on our website, press 3.", "3"),
    ("For questions about the main menu items on our website, press 3.", "3"),
    ("To go back to a previous order, press 4.", "4"),
    ("To go back to previous orders and invoices, press 4.", "4"),
    ("To return a product and get a refund, press 5.", "5"),
    ("To start a new order then track it, press 6.", "6"),
    ("To go back to your account page, press 7.", "7"),
])
def test_navigation_words_inside_a_longer_choice_are_not_replays(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("", key, prompt) is False


@pytest.mark.parametrize("prompt,key,stored", [
    ("Thank you for calling Acme, how can we help you today? Press 1.", "1", "Billing"),
    ("Did you know you can manage your account online at our website? Press 1.", "1", "Billing"),
    ("If you know your party's extension, you may dial it at any time. Press 0.", "0", "Operator"),
    ("Welcome to Acme. You can manage your account online. Press 1.", "1", "Billing"),
])
def test_a_greeting_or_disclosure_is_never_merged_into_a_label(prompt, key, stored):
    from zeroout.menu_text import fill_label
    assert fill_label(stored, key, prompt) == stored
    assert [x["key"] for x in menu_lines(prompt)][-1] == key and len(menu_lines(prompt)[-1]["text"].split()) <= 2


def test_a_very_long_merged_label_does_not_override_a_stored_one():
    from zeroout.menu_text import fill_label
    long = ("Are you calling about a very long list of things that go on and on and on and on and on? Press 1.")
    assert fill_label("Orders", "1", long) == "Orders"
    assert fill_label("", "1", long).startswith("A very long list")


@pytest.mark.parametrize("stored,want", [
    ("billing", "Billing questions"),
    ("Billing questions", "Billing questions"),
    ("Return a product", "Return a product"),
    ("one", "Billing questions"),
    ("", "Billing questions"),
])
def test_the_derived_label_wins_only_when_stored_is_bare_or_shares_a_word_or_is_shifted(stored, want):
    from zeroout.menu_text import fill_label
    assert fill_label(stored, "1", COSTCO) == want


def test_a_stored_label_that_is_another_keys_sentence_is_shifted_and_loses():
    from zeroout.menu_text import fill_label
    assert fill_label("Canada vacation packages", "4", TRAVEL) == "Travel within the United States"
    assert fill_label("all other international travel", "5", TRAVEL) == "Canada vacation packages"
    assert fill_label("Return a product", "4", TRAVEL) == "Return a product"


@pytest.mark.parametrize("prompt,key,stored,want", [
    ("If you are a new customer, press 2.", "2", "New customer", "New customer"),
    ("If you are calling about sales, press 1 for more options.", "1", "Sales", "Sales"),
    ("If you are an existing member, press 3.", "3", "", "Existing member"),
    ("If you are calling for billing, press 4.", "4", "", "Billing"),
    ("If you are calling regarding a claim, press 5.", "5", "", "A claim"),
])
def test_if_you_are_frames_are_stripped(prompt, key, stored, want):
    from zeroout.menu_text import fill_label
    assert fill_label(stored, key, prompt) == want


SUPPORT = "For billing. Press 1. For technical support. Press 2. For returns. Press 3."


def test_a_stored_label_overlapping_another_keys_label_is_shifted_not_compared_for_equality():
    from zeroout.menu_text import fill_label
    assert fill_label("tech support", "1", SUPPORT) == "Billing"
    assert fill_label("", "2", SUPPORT) == "Technical support"
    assert fill_label("tech support", "2", SUPPORT) == "Technical support"


def test_a_stored_label_that_overlaps_its_own_and_another_keys_derived_label_gets_its_own():
    from zeroout.menu_text import fill_label
    text = "For billing questions, press 1. For billing disputes, press 2."
    assert fill_label("Billing", "1", text) == "Billing questions"
    assert fill_label("Billing", "2", text) == "Billing disputes"


def test_an_unrelated_stored_label_survives_whatever_other_keys_say():
    from zeroout.menu_text import fill_label
    text = "To speak with a representative, press 0. For billing, press 1."
    assert fill_label("Agent", "0", text) == "Agent"
    assert fill_label("agent", "0", text) == "agent"


def test_punctuation_and_case_do_not_hide_a_shared_word():
    from zeroout.menu_text import fill_label
    assert fill_label("BILLING!", "1", SUPPORT) == "Billing"
    assert fill_label("the option", "1", SUPPORT) == "the option"  # only stop words: nothing shared


def test_the_travel_fixture_gives_every_key_its_derived_label_and_only_the_star_replays():
    from zeroout.menu_text import fill_label, option_replays
    want = {"1": "Rental cars", "2": "Cruises", "3": "Specialty travel or guided vacations",
            "4": "Travel within the United States", "5": "Canada vacation packages",
            "6": "All other international travel",
            "7": "Travel insurance or if you are in need of in-travel or post-travel assistance",
            "*": "Repeat these options"}
    shifted = {"3": "travel within the United States", "4": "Canada vacation packages",
               "5": "all other international travel", "6": "travel insurance or if", "7": "To repeat these options"}
    for k, label in want.items():
        assert fill_label(shifted.get(k, ""), k, TRAVEL) == label
    assert [k for k in want if option_replays(shifted.get(k, ""), k, TRAVEL)] == ["*"]


def test_a_true_flag_counts_only_when_the_models_own_label_reads_like_navigation():
    from zeroout.menu_text import option_replays
    assert option_replays("Repeat these options", "*", "Press star.", True) is True
    assert option_replays("Billing", "1", "For billing, press 1.", True) is False
    assert option_replays("Billing", "1", "", True) is False
    assert option_replays("", "1", "For billing, press 1.", True) is False


def test_a_false_flag_never_unmarks_a_replay_the_words_show():
    from zeroout.menu_text import option_replays
    assert option_replays("", "*", "To repeat these options, press the star key.", False) is True
    assert option_replays("Repeat these options", "*", "Press star.", False) is True  # the stored label is the rule
    for flag in (None, "true", 1, 0, [], {}):
        assert option_replays("", "*", "To repeat these options, press the star key.", flag) is True
        assert option_replays("", "1", "For billing, press 1.", flag) is False


def test_a_true_flag_is_checked_against_the_derived_label_not_a_shifted_stored_one():
    from zeroout.menu_text import option_replays
    assert option_replays("To repeat these options", "7", TRAVEL, True) is False
    assert option_replays("To return to the main menu", "2",
                          "For sales press 1. For support press 2. To return to the main menu press 9.", True) is False
    assert option_replays("Billing", "1", "For billing, press 1.", True) is False
    assert option_replays("Repeat these options", "*", "", True) is True
    assert option_replays("Repeat these options", "*", None, True) is True


@pytest.mark.parametrize("prompt,key", [
    ("To start over with a new booking, press 2.", "2"),
    ("For questions about the main menu items on our website, press 3.", "3"),
    ("To go back to a previous order, press 4.", "4"),
])
def test_a_true_flag_cannot_make_a_choice_a_replay_when_the_words_show_it_is_not(prompt, key):
    from zeroout.menu_text import option_replays
    assert option_replays("Start over", key, prompt, True) is False
    assert option_replays("", key, prompt, True) is False
