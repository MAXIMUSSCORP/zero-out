import json
import sqlite3
from dataclasses import asdict
from typing import Callable

from . import db
from .models import Observation

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


def merge(
    conn: sqlite3.Connection,
    company_id: int,
    path: str,
    obs: Observation,
    run_id: int,
    audio_path: str,
    same_meaning: Callable[[str, str], bool],
) -> str:
    node = db.get_node(conn, company_id, path)
    if obs.outcome == "invalid_input":
        if node is None:
            conn.execute(
                "INSERT INTO nodes (company_id, path, status, timing_correction) "
                "VALUES (?, ?, 'unexplored', 2.0)",
                (company_id, path),
            )
        else:
            conn.execute(
                "UPDATE nodes SET timing_correction = COALESCE(timing_correction, 0) + 2.0 "
                "WHERE company_id = ? AND path = ?",
                (company_id, path),
            )
        conn.commit()
        return "retry"

    ts = db.now()
    obs_id = conn.execute(
        "INSERT INTO observations (company_id, path, run_id, raw_json, audio_url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, path, run_id, json.dumps(asdict(obs)), audio_path, ts),
    ).lastrowid
    options_json = json.dumps([asdict(o) for o in obs.options])
    status = _implied_status(obs.outcome)
    result: str

    if node is None or node["status"] == "unexplored":
        conn.execute(
            "INSERT INTO nodes (company_id, path, prompt_text, options_json, outcome, "
            "prompt_seconds, status, confirmations, first_seen, last_confirmed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(company_id, path) DO UPDATE SET prompt_text=excluded.prompt_text, "
            "options_json=excluded.options_json, outcome=excluded.outcome, "
            "prompt_seconds=excluded.prompt_seconds, status=excluded.status, confirmations=1, "
            "first_seen=excluded.first_seen, last_confirmed=excluded.last_confirmed",
            (company_id, path, obs.prompt_text, options_json, obs.outcome,
             obs.prompt_seconds, status, ts, ts),
        )
        if obs.outcome == "menu":
            _add_children(conn, company_id, path, obs)
        result = "mapped" if status == "mapped" else "terminal"
    else:
        same = (
            node["outcome"] == obs.outcome
            and _keys(node["options_json"]) == {o.key for o in obs.options}
            and same_meaning(node["prompt_text"] or "", obs.prompt_text)
        )
        if same:
            conn.execute(
                "UPDATE nodes SET confirmations = confirmations + 1, last_confirmed = ?, "
                "status = ? WHERE company_id = ? AND path = ?",
                (ts, status, company_id, path),
            )
            result = "confirmed"
        else:
            conn.execute(
                "UPDATE nodes SET prompt_text = ?, options_json = ?, outcome = ?, "
                "prompt_seconds = ?, status = 'changed', confirmations = 1, last_confirmed = ? "
                "WHERE company_id = ? AND path = ?",
                (obs.prompt_text, options_json, obs.outcome, obs.prompt_seconds, ts,
                 company_id, path),
            )
            prefix = "" if path == "" else path + "."
            conn.execute(
                "UPDATE nodes SET status = 'stale' WHERE company_id = ? AND path != '' "
                "AND path != ? AND substr(path, 1, ?) = ? "
                # human nodes are never re-crawled; only Connect me re-checks them.
                "AND status NOT IN ('unexplored', 'human')",
                (company_id, path, len(prefix), prefix),
            )
            if obs.outcome == "menu":
                _add_children(conn, company_id, path, obs)
            result = "changed"
    conn.execute("UPDATE observations SET result = ? WHERE id = ?", (result, obs_id))
    conn.commit()
    return result


def unexplored(conn: sqlite3.Connection, company_id: int, max_depth: int) -> list[str]:
    rows = conn.execute(
        "SELECT path FROM nodes WHERE company_id = ? AND status IN ('unexplored', 'stale')",
        (company_id,),
    ).fetchall()
    paths = [r["path"] for r in rows if _depth(r["path"]) <= max_depth]
    return sorted(paths, key=lambda p: (_depth(p), p))


def tree_json(conn: sqlite3.Connection, company_id: int) -> dict | None:
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
        options = json.loads(r["options_json"] or "[]")
        labels = {o["key"]: o["label"] for o in options}
        children = []
        for c in sorted(kids.get(path, []), key=lambda p: p.rpartition(".")[2]):
            k = c.rpartition(".")[2]
            children.append(build(c, k, labels.get(k, "")))
        return {
            "path": path, "key": key, "label": label,
            "prompt_text": r["prompt_text"],
            "options": [{"key": o["key"], "label": o["label"]} for o in options],
            "status": r["status"], "outcome": r["outcome"],
            "confirmations": r["confirmations"], "last_confirmed": r["last_confirmed"],
            "children": children,
        }

    return build("", "", "")


def nano_same_meaning(settings, chat=None) -> Callable[[str, str], bool]:
    def same(a: str, b: str) -> bool:
        fn = chat
        if fn is None:
            from .llm import chat_json as fn
        messages = [
            {"role": "system", "content": NANO_SYSTEM},
            {"role": "user", "content": f"Prompt A: {a}\nPrompt B: {b}"},
        ]
        out = fn(settings, settings.model_nano, messages)
        v = out.get("same") if isinstance(out, dict) else None
        return v if isinstance(v, bool) else False

    return same
