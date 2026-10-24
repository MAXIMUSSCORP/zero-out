import base64
import sys
from typing import Any, Callable

from .config import Settings, load_settings
from .llm import chat_json
from .models import OUTCOMES, VALID_KEYS, Observation, Option

PROMPT = f"""You are listening to a recording of a phone call to a company's automated system.
Reply with ONLY a JSON object of this shape:
{{"prompt_text": str, "options": [{{"key": str, "label": str}}], "outcome": str, "prompt_seconds": number}}

- prompt_text: the words the system speaks, transcribed as heard (any language).
- options: each keypad choice offered ("0"-"9", "*", "#") with its label as heard. Empty if none.
- prompt_seconds: seconds from the start of the recording until the menu prompt has finished
  speaking and would accept a key press.
- outcome: exactly one of {", ".join(OUTCOMES)}, meaning:
  menu = a menu offering keypad options
  hold = hold music or "please wait" with no choices
  human = a live person answered
  voicemail = a voicemail greeting or mailbox
  hangup = the call ended or was disconnected
  invalid_input = the system rejected the last key ("invalid entry", "sorry, I didn't get that")
  needs_input = the system asks for an account number, PIN, phone number or other data
  speech_menu = the system asks the caller to say something instead of pressing keys"""


def parse_observation(raw: Any) -> Observation | None:
    if not isinstance(raw, dict) or raw.get("outcome") not in OUTCOMES:
        return None
    items = raw.get("options") or []
    if not isinstance(items, list):
        return None
    options: list[Option] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            return None
        key = str(it.get("key"))
        if key not in VALID_KEYS:
            return None
        if key not in seen:
            seen.add(key)
            options.append(Option(key, str(it.get("label", ""))))
    if raw["outcome"] == "menu" and not options:
        return None
    try:
        seconds = float(raw["prompt_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    if not seconds >= 0 or seconds == float("inf"):
        return None
    return Observation(str(raw.get("prompt_text") or ""), options, raw["outcome"], seconds)


def listen(
    settings: Settings, wav: bytes, *, chat: Callable[..., dict | None] = chat_json
) -> Observation | None:
    audio = {"data": base64.b64encode(wav).decode(), "format": "wav"}
    content = [{"type": "text", "text": PROMPT}, {"type": "input_audio", "input_audio": audio}]
    raw = chat(settings, settings.model_omni, [{"role": "user", "content": content}])
    return parse_observation(raw)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m zeroout.listen file.wav")
    with open(sys.argv[1], "rb") as f:
        print(listen(load_settings(), f.read()))
