"""Phone in hand: map a menu by calling on your own phone, on speaker, with no Twilio.

The browser records each prompt and the server hears it with the same listener as a crawl.
The person sees what was heard and keeps it or not. Microphone audio is never stored,
because it can capture the room.
"""
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import db, tree
from .listen import hear_words
from .models import Observation, Option
from .redact import redact as _redact

KEYPAD = "1234567890*#"
NOT_IN_TREE = "That menu path is not in the tree."
NOTHING_HEARD = "Nothing usable was heard. Put the phone on speaker close to the computer and try again."
UNREADABLE = "The menu was heard but could not be read. Try again."
HOLD_QUIET = "That path plays hold music or waits without a menu."
HANGUP_QUIET = "Nothing came through for 90 seconds, so that path seems to end the call."
DISPUTED = "This differs from what an automatic call heard before, so the earlier version was kept."

MESSAGES = {
    "human": "A person answered, so Zero-Out kept none of their words. This path is marked as reaching a person.",
    "voicemail": "That path reached a voicemail greeting, so Zero-Out kept none of its words.",
    "hold": "That path puts you on hold.",
    "hangup": "The call ended on that path.",
    "needs_input": "That path asks for account details, so Zero-Out stops there.",
    "speech_menu": "That menu wants you to speak instead of pressing keys, so Zero-Out stops there.",
    "invalid_input": "The menu did not accept that key.",
}
MEANING = {
    "mapped": "This is new, so it is now in the tree.",
    "confirmed": "It matches what was heard before.",
    "changed": "It differs from what was heard before, so the branches below will be checked again.",
    "undecided": "Zero-Out could not tell whether it matches what was heard before, so it kept the earlier version.",
}


@dataclass
class Step:
    path: str
    keys: list[str]
    press_now: str | None
    label: str


@dataclass
class Heard:
    obs: Observation | None
    message: str


@dataclass
class Clip:
    outcome: str | None
    options: list[Option]
    result: str | None
    message: str
    next: Step | None


def start_phone_run(conn: sqlite3.Connection, company_id: int) -> int:
    return db.start_run(conn, company_id, "phone", "phone")


def _keys(path: str) -> list[str]:
    return path.split(".") if path else []


def _order(path: str) -> list[int]:
    return [KEYPAD.index(k) if k in KEYPAD else len(KEYPAD) for k in _keys(path)]


def _label(conn: sqlite3.Connection, company_id: int, path: str) -> str:
    if path == "":
        return ""
    parent = db.get_node(conn, company_id, path.rpartition(".")[0])
    if parent is None:
        return ""
    for o in json.loads(parent["options_json"] or "[]"):
        if o["key"] == path.rpartition(".")[2]:
            return o["label"]
    return ""


def _step(conn: sqlite3.Connection, company_id: int, path: str, current: str | None) -> Step:
    is_child = current is not None and path != "" and path.rpartition(".")[0] == current
    return Step(path, _keys(path), path.rpartition(".")[2] if is_child else None,
                _label(conn, company_id, path))


def _skipped(path: str, skip) -> bool:
    parts = _keys(path)
    return any(".".join(parts[:i]) in skip for i in range(len(parts) + 1))


def next_step(
    conn: sqlite3.Connection,
    company_id: int,
    current: str | None,
    max_depth: int = 4,
    skip=frozenset(),
) -> Step | None:
    """What to map next. A path in skip, or under one, is never returned."""
    if db.get_node(conn, company_id, "") is None:  # never mapped: nothing is in the tree yet
        return None if "" in skip else Step("", [], None, "")
    paths = [p for p in tree.unexplored(conn, company_id, max_depth) if not _skipped(p, skip)]
    if not paths:
        return None
    kids = [p for p in paths if current is not None and p != "" and p.rpartition(".")[0] == current]
    best = min(kids or paths, key=lambda p: (len(_keys(p)), _order(p)))
    return _step(conn, company_id, best, current)


def retry_step(path: str) -> Step:
    return Step(path, _keys(path), None, "")


# ---------- hearing ----------

def _filtered(obs: Observation) -> Observation:
    """A microphone hears the room: keep a menu's wording, minus anything that looks like a number."""
    text = _redact(obs.prompt_text) if obs.outcome == "menu" else ""
    return Observation(text, [Option(o.key, _redact(o.label)) for o in obs.options],
                       obs.outcome, obs.prompt_seconds)


def _heard_count(count: int) -> str:
    return f"Heard {count} option{'' if count == 1 else 's'}."


def hear_clip(
    settings,
    wav: bytes,
    *,
    capped: bool,
    sound: bool,
    hear: Callable[..., tuple[bool, Observation | None]] = hear_words,
) -> Heard:
    """RuntimeError (model missing) and ValueError (bad audio) propagate."""
    recognized, obs = hear(settings, wav)
    if not recognized:
        if capped and sound:
            return Heard(Observation("", [], "hold", 0.0), HOLD_QUIET)
        if capped:
            return Heard(Observation("", [], "hangup", 0.0), HANGUP_QUIET)
        return Heard(None, NOTHING_HEARD)
    if obs is None:
        return Heard(None, UNREADABLE)
    obs = _filtered(obs)
    if obs.outcome == "menu":
        return Heard(obs, _heard_count(len(obs.options)))
    return Heard(obs, MESSAGES[obs.outcome])


def keep_clip(
    conn: sqlite3.Connection,
    company_id: int,
    path: str,
    obs: Observation,
    *,
    run_id: int,
    same_meaning: Callable[[str, str], bool | None],
) -> Clip:
    tree.ensure_root(conn, company_id)
    if path != "" and db.get_node(conn, company_id, path) is None:
        raise ValueError(NOT_IN_TREE)
    result = tree.merge(conn, company_id, path, obs, run_id, None, same_meaning, source="phone")
    if result == "disputed":
        message = DISPUTED
    elif obs.outcome == "menu":
        message = f"{_heard_count(len(obs.options))} {MEANING.get(result, '')}".rstrip()
    else:
        message = MESSAGES[obs.outcome]
    return Clip(obs.outcome, obs.options, result, message, next_step(conn, company_id, path))
