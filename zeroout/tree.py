import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

from . import db
from .menu_text import fill_label, replays_menu
from .models import Observation

MAX_CORRECTION = 10.0
# Sources that are not this machine's own automatic calls. They fill and confirm, never overwrite or delete.
GUEST_SOURCES = ("phone", "community")
# Whose data each guest source must not overwrite: a community file protects the user's crawls and phone clips.
OWNED_BY = {"phone": ("crawl",), "community": ("crawl", "phone")}
TERMINAL = ("human", "needs_input", "speech_menu")

NANO_SYSTEM = (
    "You compare two phone IVR prompts from the same menu position. Answer whether they "
    "mean the same thing, ignoring wording, filler, greetings and promotions. "
    'Reply with JSON only: {"same": true} or {"same": false}.'
)


def _child_path(parent: str, key: str) -> str:
    return key if parent == "" else f"{parent}.{key}"


def _depth(path: str) -> int:
    return 0 if path == "" else path.count(".") + 1


def _implied_status(outcome: str) -> str:
    return outcome if outcome in TERMINAL else "mapped"


def ensure_root(conn: sqlite3.Connection, company_id: int) -> None:
    with db.LOCK:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (company_id, path, status) VALUES (?, '', 'unexplored')",
            (company_id,),
        )
        conn.commit()


def _add_children(conn: sqlite3.Connection, cid: int, path: str, obs: Observation) -> None:
    for o in obs.options:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (company_id, path, status) VALUES (?, ?, 'unexplored')",
            (cid, _child_path(path, o.key)),
        )


def _keys(options_json: str | None) -> set[str]:
    return {o["key"] for o in json.loads(options_json or "[]")}


def _revise_descendants(conn: sqlite3.Connection, cid: int, path: str, kept: set[str]) -> None:
    """Under a changed node: delete subtrees of removed keys, mark the rest stale."""
    prefix = "" if path == "" else path + "."
    rows = conn.execute(
        "SELECT path, status FROM nodes WHERE company_id = ? AND path != '' AND path != ? "
        "AND substr(path, 1, ?) = ?",
        (cid, path, len(prefix), prefix),
    ).fetchall()
    for r in rows:
        if r["path"][len(prefix):].split(".", 1)[0] not in kept:
            conn.execute("DELETE FROM nodes WHERE company_id = ? AND path = ?", (cid, r["path"]))
        elif r["status"] not in ("unexplored", "human"):  # human nodes are only re-checked by Connect me
            conn.execute(
                "UPDATE nodes SET status = 'stale' WHERE company_id = ? AND path = ?",
                (cid, r["path"]),
            )


def _protected(source: str, node) -> bool:
    """A guest source may confirm or dispute these nodes, never change them."""
    if node is None or source not in OWNED_BY or node["source"] not in OWNED_BY[source]:
        return False
    if source == "community":  # a stale node is still the user's own data
        return node["status"] != "unexplored"
    return node["status"] not in ("unexplored", "stale")


def _when(text: str) -> datetime:
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _older_than_stored(conn: sqlite3.Connection, cid: int, path: str, heard_at: str) -> bool:
    rows = conn.execute(
        "SELECT created_at FROM observations WHERE company_id = ? AND path = ?", (cid, path)
    ).fetchall()
    return any(_when(r["created_at"]) > _when(heard_at) for r in rows)


def _owned_below(conn: sqlite3.Connection, cid: int, path: str, obs: Observation, node, source: str) -> bool:
    """True when this change would alter keys over nodes the user's own calls have mapped."""
    kept = {o.key for o in obs.options} if obs.outcome == "menu" else set()
    if kept == _keys(node["options_json"]):
        return False
    prefix = "" if path == "" else path + "."
    rows = conn.execute(
        "SELECT path FROM nodes WHERE company_id = ? AND path != '' "
        f"AND source IN ({', '.join('?' * len(OWNED_BY[source]))}) "
        "AND status != 'unexplored' AND substr(path, 1, ?) = ?",
        (cid, *OWNED_BY[source], len(prefix), prefix),
    ).fetchall()
    return bool(rows)


def merge(
    conn: sqlite3.Connection,
    company_id: int,
    path: str,
    obs: Observation,
    run_id: int,
    audio_path: str | None,
    same_meaning: Callable[[str, str], bool | None],
    source: str = "crawl",
    *,
    origin: str = "local",
    uid: str | None = None,
    heard_at: str | None = None,
) -> str:
    # The model call runs before the lock so a slow answer never blocks the site.
    meaning: bool | None = None
    node = db.get_node(conn, company_id, path)
    if (
        obs.outcome != "invalid_input"
        and node is not None
        and node["status"] != "unexplored"
        and node["outcome"] == obs.outcome
        and _keys(node["options_json"]) == {o.key for o in obs.options}
    ):
        meaning = same_meaning(node["prompt_text"] or "", obs.prompt_text)
    with db.LOCK:
        try:
            result = _merge_locked(
                conn, company_id, path, obs, run_id, audio_path, meaning, source, origin, uid, heard_at)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    return result


def _merge_locked(
    conn: sqlite3.Connection,
    company_id: int,
    path: str,
    obs: Observation,
    run_id: int,
    audio_path: str | None,
    meaning: bool | None,
    source: str = "crawl",
    origin: str = "local",
    uid: str | None = None,
    heard_at: str | None = None,
) -> str:
    node = db.get_node(conn, company_id, path)
    if obs.outcome == "invalid_input":
        if source in GUEST_SOURCES:  # a person's own wrong key says nothing about the automatic replay's timing
            return "retry"
        if node is None:
            conn.execute(
                "INSERT INTO nodes (company_id, path, status, timing_correction) "
                "VALUES (?, ?, 'unexplored', 2.0)",
                (company_id, path),
            )
        else:
            conn.execute(
                "UPDATE nodes SET timing_correction = MIN(COALESCE(timing_correction, 0) + 2.0, ?) "
                "WHERE company_id = ? AND path = ?",
                (MAX_CORRECTION, company_id, path),
            )
        return "retry"

    ts = db.now()
    obs_id = conn.execute(
        "INSERT INTO observations (company_id, path, run_id, raw_json, audio_url, created_at, origin, uid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (company_id, path, run_id, json.dumps(asdict(obs)), audio_path, heard_at or ts, origin, uid),
    ).lastrowid
    options_json = json.dumps([asdict(o) for o in obs.options])
    status = _implied_status(obs.outcome)
    result: str
    # A phone clip or a community file never overwrites what an automatic call heard here: it can only
    # confirm it or dispute it.
    protected = _protected(source, node)
    older = source == "community" and heard_at is not None and _older_than_stored(conn, company_id, path, heard_at)
    same_shape = (
        node is not None and node["outcome"] == obs.outcome
        and _keys(node["options_json"]) == {o.key for o in obs.options}
    )

    if protected and not (same_shape and meaning):
        result = "disputed"
    elif protected:
        conn.execute(
            "UPDATE nodes SET confirmations = confirmations + 1, last_confirmed = ?, "
            "status = ? WHERE company_id = ? AND path = ?",
            (ts, status, company_id, path),
        )
        result = "confirmed"
    elif node is None or node["status"] == "unexplored":
        conn.execute(
            "INSERT INTO nodes (company_id, path, prompt_text, options_json, outcome, "
            "prompt_seconds, status, confirmations, first_seen, last_confirmed, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(company_id, path) DO UPDATE SET prompt_text=excluded.prompt_text, "
            "options_json=excluded.options_json, outcome=excluded.outcome, "
            "prompt_seconds=excluded.prompt_seconds, status=excluded.status, confirmations=1, "
            "first_seen=excluded.first_seen, last_confirmed=excluded.last_confirmed, "
            "source=excluded.source",
            (company_id, path, obs.prompt_text, options_json, obs.outcome,
             obs.prompt_seconds, status, ts, ts, source),
        )
        if obs.outcome == "menu":
            _add_children(conn, company_id, path, obs)
        result = "mapped" if status == "mapped" else "terminal"
    elif (
        node["outcome"] == obs.outcome
        and _keys(node["options_json"]) == {o.key for o in obs.options}
        and meaning is None
    ):
        result = "undecided"
    elif (
        node["outcome"] == obs.outcome
        and _keys(node["options_json"]) == {o.key for o in obs.options}
        and meaning
    ):
        conn.execute(
            "UPDATE nodes SET confirmations = confirmations + 1, last_confirmed = ?, "
            "status = ?, source = ? WHERE company_id = ? AND path = ?",
            (ts, status, source, company_id, path),
        )
        result = "confirmed"
    elif source in GUEST_SOURCES and _owned_below(conn, company_id, path, obs, node, source):
        result = "disputed"
    elif older:  # an old file may confirm what is stored, never change it
        result = "disputed"
    else:
        conn.execute(
            "UPDATE nodes SET prompt_text = ?, options_json = ?, outcome = ?, "
            "prompt_seconds = ?, status = 'changed', confirmations = 1, last_confirmed = ?, "
            "source = ? WHERE company_id = ? AND path = ?",
            (obs.prompt_text, options_json, obs.outcome, obs.prompt_seconds, ts, source,
             company_id, path),
        )
        kept = {o.key for o in obs.options} if obs.outcome == "menu" else set()
        _revise_descendants(conn, company_id, path, kept)
        if obs.outcome == "menu":
            _add_children(conn, company_id, path, obs)
        result = "changed"
    if source == "community" and result == "undecided":  # not final: keep no row so the next update asks again
        conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
        return result
    conn.execute("UPDATE observations SET result = ? WHERE id = ?", (result, obs_id))
    return result


def unexplored(conn: sqlite3.Connection, company_id: int, max_depth: int) -> list[str]:
    """Nodes still to hear, shallowest first. An option whose label only replays a menu is never
    one of them, nor is anything under it."""
    with db.LOCK:
        rows = {r["path"]: r for r in conn.execute(
            "SELECT path, status, options_json, prompt_text FROM nodes WHERE company_id = ?", (company_id,))}

    def label(path: str) -> str:
        parent = rows.get(path.rpartition(".")[0])
        for o in json.loads(parent["options_json"] or "[]") if parent else ():
            if o["key"] == path.rpartition(".")[2]:
                return fill_label(o["label"], o["key"], parent["prompt_text"])
        return ""

    def replay(path: str) -> bool:
        parts = path.split(".")
        return any(replays_menu(label(".".join(parts[:i]))) for i in range(1, len(parts) + 1))

    paths = [p for p, r in rows.items()
             if r["status"] in ("unexplored", "stale") and p != "" and _depth(p) <= max_depth and not replay(p)]
    if "" in rows and rows[""]["status"] in ("unexplored", "stale") and _depth("") <= max_depth:
        paths.append("")
    return sorted(paths, key=lambda p: (_depth(p), p))


def tree_json(conn: sqlite3.Connection, company_id: int) -> dict | None:
    with db.LOCK:
        rows = {
            r["path"]: r
            for r in conn.execute("SELECT * FROM nodes WHERE company_id = ?", (company_id,))
        }
    if "" not in rows:
        return None
    kids: dict[str, list[str]] = {}
    for p in rows:
        if p != "":
            kids.setdefault(p.rpartition(".")[0], []).append(p)

    def build(path: str, key: str, label: str) -> dict:
        r = rows[path]
        options = [{"key": o["key"], "label": fill_label(o["label"], o["key"], r["prompt_text"])}
                   for o in json.loads(r["options_json"] or "[]")]
        labels = {o["key"]: o["label"] for o in options}
        children = []
        for c in sorted(kids.get(path, []), key=lambda p: p.rpartition(".")[2]):
            k = c.rpartition(".")[2]
            children.append(build(c, k, labels.get(k, "")))
        return {
            "path": path, "key": key, "label": label,
            "prompt_text": r["prompt_text"],
            "options": options,
            "replays": replays_menu(label),
            "status": r["status"], "outcome": r["outcome"],
            "confirmations": r["confirmations"], "last_confirmed": r["last_confirmed"],
            "children": children,
        }

    return build("", "", "")


def nano_same_meaning(settings, chat=None) -> Callable[[str, str], bool | None]:
    """True/False when the model answers; None when it gives no usable answer."""
    def same(a: str, b: str) -> bool | None:
        fn = chat
        if fn is None:
            from .llm import chat_json as fn
        messages = [
            {"role": "system", "content": NANO_SYSTEM},
            {"role": "user", "content": f"Prompt A: {a}\nPrompt B: {b}"},
        ]
        try:
            out = fn(settings, settings.model_nano, messages)
        except Exception:
            return None
        v = out.get("same") if isinstance(out, dict) else None
        return v if isinstance(v, bool) else None

    return same
