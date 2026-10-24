import base64
import json

import httpx
import openai

from zeroout.config import Settings
from zeroout.listen import listen, parse_observation
from zeroout.llm import chat_json
from zeroout.models import OUTCOMES, Observation, Option


def raw(**kw) -> dict:
    d = {
        "prompt_text": "For billing press 1",
        "options": [{"key": "1", "label": "billing"}],
        "outcome": "menu",
        "prompt_seconds": 6.2,
    }
    d.update(kw)
    return d


def make_settings() -> Settings:
    return Settings(
        tf_api_key="k", tf_base_url="http://x", model_omni="omni-id", model_nano="nano-id",
        model_super="super-id", twilio_sid="", twilio_token="", twilio_from="",
        twilio_verify_sid="", daily_cap_usd=1.0, session_secret="s",
    )


def test_parse_valid():
    o = parse_observation(raw())
    assert o.options == [Option("1", "billing")] and o.outcome == "menu"
    assert o.prompt_text == "For billing press 1" and o.prompt_seconds == 6.2


def test_parse_rejects_bad_key():
    assert parse_observation(raw(options=[{"key": "A", "label": "x"}])) is None


def test_parse_rejects_unknown_outcome():
    assert parse_observation(raw(outcome="robot")) is None


def test_menu_without_options_is_unparsed():
    assert parse_observation(raw(options=[], outcome="menu")) is None


def test_duplicate_keys_first_wins():
    o = parse_observation(raw(options=[{"key": "1", "label": "billing"}, {"key": "1", "label": "sales"}]))
    assert o.options == [Option("1", "billing")]


def test_non_english_label_kept():
    o = parse_observation(raw(options=[{"key": "2", "label": "español"}]))
    assert o.options[0].label == "español"


def test_integer_keys_normalised():
    o = parse_observation(raw(options=[{"key": 1, "label": "a"}, {"key": 2, "label": "b"}]))
    assert [x.key for x in o.options] == ["1", "2"]


def test_non_dict_rejected():
    for bad in (None, [], "menu", 3):
        assert parse_observation(bad) is None


def test_prompt_seconds_coerced_or_rejected():
    assert parse_observation(raw(prompt_seconds="4")).prompt_seconds == 4.0
    assert parse_observation(raw(prompt_seconds=-1)) is None
    assert parse_observation(raw(prompt_seconds="soon")) is None
    d = raw()
    del d["prompt_seconds"]
    assert parse_observation(d) is None


def test_non_menu_without_options_ok():
    o = parse_observation(raw(options=[], outcome="human", prompt_seconds=0))
    assert o.outcome == "human" and o.options == []


def test_malformed_options_rejected():
    assert parse_observation(raw(options="1")) is None
    assert parse_observation(raw(options=["1"])) is None


# --- chat_json with a fake client ---

class FakeClient:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = type("M", (), {"content": item})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


def rate_limit(headers=None) -> openai.RateLimitError:
    resp = httpx.Response(429, headers=headers or {}, request=httpx.Request("POST", "http://x"))
    return openai.RateLimitError("slow down", response=resp, body=None)


MSGS = [{"role": "user", "content": "hi"}]


def test_chat_json_ok():
    c = FakeClient(['{"a": 1}'])
    assert chat_json(make_settings(), "m", MSGS, client=c) == {"a": 1}
    assert c.calls[0]["model"] == "m"
    assert c.calls[0]["response_format"] == {"type": "json_object"}


def test_chat_json_retries_once_on_bad_json():
    c = FakeClient(["nope", '{"a": 1}'])
    assert chat_json(make_settings(), "m", MSGS, client=c) == {"a": 1}
    assert len(c.calls) == 2
    assert c.calls[1]["messages"][-1] == {"role": "system", "content": "Return ONLY valid JSON."}
    assert len(MSGS) == 1


def test_chat_json_gives_up_after_one_json_retry():
    c = FakeClient(["nope", "still nope", '{"a": 1}'])
    assert chat_json(make_settings(), "m", MSGS, client=c) is None
    assert len(c.calls) == 2


def test_chat_json_non_dict_is_failure():
    c = FakeClient(["[1]", "[2]"])
    assert chat_json(make_settings(), "m", MSGS, client=c) is None


def test_chat_json_none_content():
    c = FakeClient([None, None])
    assert chat_json(make_settings(), "m", MSGS, client=c) is None


def test_chat_json_429_honours_retry_after_then_succeeds():
    c = FakeClient([rate_limit({"retry-after": "5"}), '{"a": 1}'])
    slept = []
    assert chat_json(make_settings(), "m", MSGS, client=c, sleep=slept.append) == {"a": 1}
    assert slept == [5.0]


def test_chat_json_429_default_doubles_and_gives_up_after_3():
    c = FakeClient([rate_limit(), rate_limit(), rate_limit(), '{"a": 1}'])
    slept = []
    assert chat_json(make_settings(), "m", MSGS, client=c, sleep=slept.append) is None
    assert len(c.calls) == 3
    assert slept == [2.0, 4.0]


# --- listen ---

def test_listen_builds_audio_message_and_parses():
    seen = {}

    def fake_chat(settings, model, messages):
        seen.update(model=model, messages=messages)
        return raw()

    wav = b"RIFFfake"
    o = listen(make_settings(), wav, chat=fake_chat)
    assert isinstance(o, Observation) and o.options == [Option("1", "billing")]
    assert seen["model"] == "omni-id"
    (msg,) = seen["messages"]
    assert msg["role"] == "user"
    text, audio = msg["content"]
    assert text["type"] == "text"
    for name in OUTCOMES:
        assert name in text["text"]
    for field in ("prompt_text", "options", "outcome", "prompt_seconds"):
        assert field in text["text"]
    assert audio == {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"},
    }
    json.dumps(seen["messages"])


def test_listen_returns_none_when_chat_fails_or_unparsable():
    assert listen(make_settings(), b"x", chat=lambda s, m, ms: None) is None
    assert listen(make_settings(), b"x", chat=lambda s, m, ms: raw(outcome="robot")) is None
