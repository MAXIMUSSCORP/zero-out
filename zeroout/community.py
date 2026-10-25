"""Sharing what you mapped. A contribution is one file of settled observations, text only: no audio,
no costs, no phone number of yours, and no id that links one file to the next. Import replays other
people's files through the same merge, but only for companies you added yourself."""
import json
import re
import secrets
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from . import db, tree
from .community_check import FORMAT, MAX_DEPTH, MAX_LABEL, MAX_NAME, MAX_OBSERVATIONS, MAX_TEXT
from .community_check import DIGITS, KEYS, OUTCOMES, check, observation_uid
from .models import Observation, Option
from .redact import redact

SETTLED = ("mapped", "confirmed", "changed", "terminal")
BLANK_OUTCOMES = ("human", "voicemail")  # a person's greeting or name is never shared
MAX_MODEL_CHECKS = 200
MAX_NEW_NODES = 2000  # per company per import


@dataclass
class Contribution:
    relpath: str
    doc: dict
    body: bytes
    observation_ids: list[int]
    file_id: str


@dataclass
class ImportReport:
    files: int = 0  # files offered, valid or not
    invalid: list[str] = field(default_factory=list)
    offered: int = 0  # numbers with trees in the files that you have not added
    truncated: bool = False  # the download was cut short; passed in by whoever fetched the files
    capped: bool = False  # a company hit the new-node cap
    orphans: int = 0  # observations whose parent menu is not in the tree or does not offer that key
    future: int = 0  # observations dated after tomorrow, which are ignored
    observations_added: int = 0
    already_had: int = 0
    results: dict[str, int] = field(default_factory=dict)


def new_file_id() -> str:
    """A fresh random id for every file, so two files from one machine cannot be linked."""
    return secrets.token_hex(8)


def _strip(text: str) -> str:
    """Drop control and invisible formatting characters."""
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf"))


def _when(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _day(stamp: str) -> str:
    return _when(stamp).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _depth(path: str) -> int:
    return 0 if path == "" else path.count(".") + 1


def _doc_for(row: sqlite3.Row) -> dict | None:
    """The shareable form of one observation, or None when it cannot be shared as it is."""
    try:
        raw = json.loads(row["raw_json"])
        outcome = raw["outcome"]
        seconds = raw["prompt_seconds"]
        keys = [o["key"] for o in raw.get("options") or []]
    except (ValueError, KeyError, TypeError):
        return None
    if outcome not in OUTCOMES or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not 0 <= seconds <= 120 or any(k not in KEYS for k in keys) or len(set(keys)) != len(keys):
        return None
    blank = outcome in BLANK_OUTCOMES
    options = [
        {"key": o["key"], "label": "" if blank else redact(_strip(str(o.get("label") or "")))[:MAX_LABEL]}
        for o in raw.get("options") or []
    ]
    text = "" if blank else redact(_strip(str(raw.get("prompt_text") or "")))[:MAX_TEXT]
    if DIGITS.search(text) or any(DIGITS.search(o["label"]) for o in options):
        return None
    return {
        "path": row["path"],
        "prompt_text": text,
        "options": options,
        "outcome": outcome,
        "prompt_seconds": seconds,
        "heard_at": _day(row["created_at"]),
        "source": "phone" if row["kind"] == "phone" else "crawl",
    }


def _clean_name(name: str, number: str) -> str:
    text = redact(" ".join(_strip(name).split()))[:MAX_NAME].strip()
    return text or number


def build_contribution(
    conn: sqlite3.Connection, company_id: int, *, now: datetime | None = None
) -> Contribution | None:
    """None when this company has nothing settled and unshared. At most 500 observations go in one file."""
    with db.LOCK:
        company = conn.execute("SELECT name, number FROM companies WHERE id = ?", (company_id,)).fetchone()
        rows = conn.execute(
            "SELECT o.id, o.path, o.raw_json, o.created_at, r.kind FROM observations o "
            "LEFT JOIN runs r ON r.id = o.run_id "
            "WHERE o.company_id = ? AND o.origin = 'local' AND o.contributed_at IS NULL "
            f"AND o.result IN ({', '.join('?' * len(SETTLED))})",
            (company_id, *SETTLED),
        ).fetchall()
    rows = [r for r in rows if _depth(r["path"]) <= MAX_DEPTH]
    rows.sort(key=lambda r: (_when(r["created_at"]), r["id"]))  # stored stamps differ in format
    docs = [(r["id"], d) for r in rows if (d := _doc_for(r)) is not None][:MAX_OBSERVATIONS]
    if company is None or not docs:
        return None
    number = company["number"].removeprefix("+1")
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = when.strftime("%Y-%m-%d")
    doc = {
        "format": FORMAT,
        "number": number,
        "name": _clean_name(company["name"], number),
        "contributed_at": day,
        "observations": [d for _, d in docs],
    }
    body = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    file_id = new_file_id()
    relpath = f"trees/{number}/{day}-{file_id}.json"
    problems = check(relpath, body)
    if problems:
        raise ValueError("The contribution failed its own check, which is a bug: " + " ".join(problems))
    return Contribution(relpath, doc, body, [i for i, _ in docs], file_id)


def mark_contributed(conn: sqlite3.Connection, observation_ids: list[int], when: str) -> None:
    """Record the share, and give each observation the uid its file carries so our own file imports as already had."""
    with db.LOCK:
        for oid in observation_ids:
            row = conn.execute(
                "SELECT o.id, o.path, o.raw_json, o.created_at, r.kind, c.number FROM observations o "
                "JOIN companies c ON c.id = o.company_id LEFT JOIN runs r ON r.id = o.run_id WHERE o.id = ?",
                (oid,),
            ).fetchone()
            if row is None:
                continue
            conn.execute("UPDATE observations SET contributed_at = ? WHERE id = ?", (when, oid))
            doc = _doc_for(row)
            if doc is None:
                continue
            uid = observation_uid(row["number"].removeprefix("+1"), doc)
            conn.execute(
                "UPDATE observations SET uid = ? WHERE id = ? AND uid IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM observations WHERE uid = ?)",
                (uid, oid, uid),
            )
        conn.commit()


def _words(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def _limited(
    same_meaning: Callable[[str, str], bool | None], limit: int
) -> Callable[[str, str], bool | None]:
    """Equal wording needs no model; past the limit the answer is undecided, not a guess."""
    calls = 0

    def same(a: str, b: str) -> bool | None:
        nonlocal calls
        if _words(a) == _words(b):
            return True
        if calls >= limit:
            return None
        calls += 1
        return same_meaning(a, b)

    return same


def list_offers(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Numbers other people mapped that you have not added. Adding one lets the next update import it."""
    with db.LOCK:
        rows = conn.execute(
            "SELECT number, name, files, first_seen, last_seen FROM offers "
            "WHERE '+1' || number NOT IN (SELECT number FROM companies) "
            "ORDER BY files DESC, last_seen DESC, number LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def import_files(
    conn: sqlite3.Connection,
    files: Iterable[tuple[str, bytes]],
    *,
    same_meaning: Callable[[str, str], bool | None],
    max_model_checks: int = MAX_MODEL_CHECKS,
    truncated: bool = False,
    today: date | None = None,
) -> ImportReport:
    """Every file is checked first, then each number's observations are replayed oldest first across all
    files. Numbers you have not added are only noted as offers."""
    report = ImportReport(truncated=truncated)
    newest = ((today or datetime.now(timezone.utc).date()) + timedelta(days=1)).isoformat()
    same = _limited(same_meaning, max_model_checks)
    groups: dict[str, dict] = {}
    for relpath, body in files:
        report.files += 1
        problems = check(relpath, body)
        if problems:
            report.invalid.append(f"{relpath}: {problems[0]}")
            continue
        doc = json.loads(body)
        g = groups.setdefault(doc["number"], {"names": Counter(), "files": 0, "obs": []})
        g["names"][doc["name"]] += 1
        g["files"] += 1
        for o in doc["observations"]:
            if o["heard_at"] > newest:  # a far-future date would make every real observation look older
                report.future += 1
            else:
                g["obs"].append(o)
    offered: set[str] = set()
    for number in sorted(groups):
        g = groups[number]
        with db.LOCK:
            row = conn.execute("SELECT id FROM companies WHERE number = ?", ("+1" + number,)).fetchone()
        if row is None:
            name = sorted(g["names"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            _offer(conn, number, name, g["files"])
            offered.add(number)
            report.offered += 1
            continue
        observations = sorted(g["obs"], key=lambda o: (o["heard_at"], _depth(o["path"]), o["path"]))
        _replay(conn, row["id"], number, observations, same, report)
    if not truncated:  # a cut-short download proves nothing about what is missing
        _keep_only_offers(conn, offered)
    return report


def _keep_only_offers(conn: sqlite3.Connection, numbers: set[str]) -> None:
    with db.LOCK:
        for (number,) in conn.execute("SELECT number FROM offers").fetchall():
            if number not in numbers:
                conn.execute("DELETE FROM offers WHERE number = ?", (number,))
        conn.commit()


def _offer(conn: sqlite3.Connection, number: str, name: str, files: int) -> None:
    stamp = db.now()
    with db.LOCK:
        conn.execute(
            "INSERT INTO offers (number, name, files, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(number) DO UPDATE SET name = excluded.name, files = excluded.files, "
            "last_seen = excluded.last_seen",
            (number, name, files, stamp, stamp),
        )
        conn.commit()


def _node_count(conn: sqlite3.Connection, company_id: int) -> int:
    with db.LOCK:
        return conn.execute("SELECT COUNT(*) FROM nodes WHERE company_id = ?", (company_id,)).fetchone()[0]


def _parent_offers_key(conn: sqlite3.Connection, company_id: int, path: str) -> bool:
    parent_path, _, key = path.rpartition(".")
    parent = db.get_node(conn, company_id, parent_path)
    if parent is None or parent["status"] == "unexplored":
        return False
    return key in {o["key"] for o in json.loads(parent["options_json"] or "[]")}


def _replay(conn, company_id: int, number: str, observations: list[dict], same, report: ImportReport) -> None:
    tree.ensure_root(conn, company_id)
    start = _node_count(conn, company_id)
    run_id: int | None = None  # made when the first new observation arrives
    for o in observations:
        uid = observation_uid(number, o)
        with db.LOCK:
            seen = conn.execute("SELECT 1 FROM observations WHERE uid = ?", (uid,)).fetchone()
        if seen:
            report.already_had += 1
            continue
        if o["path"] != "" and not _parent_offers_key(conn, company_id, o["path"]):
            report.orphans += 1  # a file may fill the user's tree, never graft a branch onto it
            continue
        if _node_count(conn, company_id) - start >= MAX_NEW_NODES:
            report.capped = True
            break
        if run_id is None:
            run_id = db.start_run(conn, company_id, "community", "community")
        obs = Observation(
            redact(o["prompt_text"]),
            [Option(p["key"], redact(p["label"])) for p in o["options"]],
            o["outcome"], float(o["prompt_seconds"]),
        )
        result = tree.merge(conn, company_id, o["path"], obs, run_id, None, same, "community",
                            origin="community", uid=uid, heard_at=o["heard_at"])
        report.results[result] = report.results.get(result, 0) + 1
        if result not in ("retry", "undecided"):  # those keep no row, so the next update tries again
            report.observations_added += 1
    if run_id is not None:
        with db.LOCK:
            conn.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (db.now(), run_id))
            conn.commit()
