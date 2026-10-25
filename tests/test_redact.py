import pytest

from zeroout import phone
from zeroout.models import Observation, Option
from zeroout.redact import redact


@pytest.mark.parametrize("said,kept", [
    ("pin 4, 5, 6, 7", "pin [number]"),
    ("call (415) 555-1234 now", "call ([number] now"),
    ("call 415, 555, 1234 now", "call [number] now"),
    ("call 415/555/1234", "call [number]"),
    ("call 415.555.1234", "call [number]"),
    ("call 415-555-1234", "call [number]"),
    ("code oh one two three", "code [number]"),
    ("code ZERO oh nine nine", "code [number]"),
    ("uno dos tres cuatro", "[number]"),
    ("mail help@acme.ca", "mail [email]"),
    ("account 12345678", "account [number]"),
])
def test_redacts(said, kept):
    assert redact(said) == kept


@pytest.mark.parametrize("text", [
    "open 24 hours", "press 1 2 3", "oh no one knows", "one moment please", "room 12 and 34", "1, 2, 3",
])
def test_leaves_short_runs_alone(text):
    assert redact(text) == text


def test_fullwidth_digits_are_not_digits_here():
    assert redact("１２３４") == "１２３４"


def test_press_list_in_prompt_text_is_redacted_but_keys_are_not():
    """The accepted trade-off: a spoken list of four keys looks like a number, so the words go. The keys stay."""
    obs = Observation("For sales press 1, 2, 3, 4", [Option(k, f"opt{k}") for k in "1234"], "menu", 2.0)
    kept = phone._filtered(obs)
    assert kept.prompt_text == "For sales press [number]"
    assert [o.key for o in kept.options] == ["1", "2", "3", "4"]


@pytest.mark.parametrize("space", [" ", " ", " ", " ", " ", " ", "　"])
def test_unicode_spaces_between_digits_are_redacted(space):
    assert redact(f"call 415{space}555{space}1234 now") == "call [number] now"
    assert redact(f"one{space}two{space}three{space}four") == "[number]"
