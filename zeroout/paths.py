import re
import sqlite3
from itertools import accumulate

from zeroout.db import get_node
from zeroout.models import Step

_KEYPAD = {c: d for d, letters in {
    "2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL",
    "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ",
}.items() for c in letters}
_TOLL_FREE = {"800", "888", "877", "866", "855", "844", "833"}
_ERR = "Enter a US number like 1-800-555-1234"


def normalize_number(raw: str) -> str:
    digits = re.sub(r"\D", "", "".join(_KEYPAD.get(c, c) for c in (raw or "").upper()))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or digits[0] != "1":
        raise ValueError(_ERR)
    return "+" + digits


def is_toll_free(e164: str) -> bool:
    return bool(re.fullmatch(r"\+1\d{10}", e164 or "")) and e164[2:5] in _TOLL_FREE


def _parent(path: str) -> str:
    return path.rpartition(".")[0]


def _num(node: sqlite3.Row | None, col: str) -> float:
    return (node[col] if node is not None else None) or 0.0


def build_path(conn: sqlite3.Connection, company_id: int, path: str, margin: float = 1.5) -> list[Step]:
    steps = []
    keys = path.split(".") if path else []
    for i, key in enumerate(keys):
        p = ".".join(keys[: i + 1])
        parent = get_node(conn, company_id, _parent(p))
        node = get_node(conn, company_id, p)
        steps.append(Step(key, _num(parent, "prompt_seconds") + margin + _num(node, "timing_correction")))
    return steps


def press_times(steps: list[Step]) -> list[float]:
    return list(accumulate(s.wait for s in steps))
