"""Seconds of menu Connect me sits through for the caller, derived from the tree: python -m eval.time_saved [--db PATH] [--csv OUT]

One row per mapped node below the root of every company Connect me can dial (a toll-free number).

connect_seconds: from the company answering to the moment Connect me rings the caller. It is what
  _run_connect sleeps: the last press time of the steps start_connect builds (build_path: each parent's
  prompt_seconds + KEY_MARGIN + the node's timing_correction, rounded up to 0.5 s by press_times)
  plus LEAD_SECONDS. Connect me waits out every prompt in real time, then rings.
manual_seconds: the same path done by hand, the recorded prompt_seconds of each parent prompt summed.
  It has no margin, correction or rounding, and none of the time a person takes to react.
extra_seconds: connect minus manual. Usually positive, since Connect me adds margin and rounding. It
  goes negative when a community-sourced parent prompt is longer than COMMUNITY_PROMPT_CAP: build_path
  caps that wait (community timing is only a hint), so Connect me presses before the long menu ends
  and the manual figure, which sums the recorded prompt_seconds uncapped, is the larger. Those rows
  are real and are kept. Where it is positive Connect me does not beat the clock either: what it saves
  the caller is the listening, since they pick up when the menu is already done.
"""
import argparse
import csv
import sqlite3
import statistics

from zeroout.config import load_settings
from zeroout.connect import LEAD_SECONDS
from zeroout.db import connect, get_node
from zeroout.paths import build_path, is_toll_free, press_times


def rows(conn: sqlite3.Connection, margin: float = 1.5) -> list[dict]:
    out = []
    for c in conn.execute("SELECT id, name, number FROM companies ORDER BY name"):
        if not is_toll_free(c["number"]):
            continue
        nodes = conn.execute(
            "SELECT path FROM nodes WHERE company_id = ? AND path != '' AND status != 'unexplored' ORDER BY path",
            (c["id"],),
        ).fetchall()
        for n in nodes:
            keys = n["path"].split(".")
            parents = [get_node(conn, c["id"], ".".join(keys[:i])) for i in range(len(keys))]
            manual = sum((p["prompt_seconds"] if p is not None else 0) or 0 for p in parents)
            connect_s = press_times(build_path(conn, c["id"], n["path"], margin))[-1] + LEAD_SECONDS
            out.append({"company": c["name"], "path": n["path"], "connect_seconds": connect_s,
                        "manual_seconds": manual, "extra_seconds": connect_s - manual})
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m eval.time_saved")
    ap.add_argument("--db", help="database to read (default: DB_PATH from settings)")
    ap.add_argument("--csv", metavar="OUT")
    args = ap.parse_args(argv)
    settings = load_settings()
    data = rows(connect(args.db or settings.db_path), settings.key_margin)
    print(f"{'company':<28}{'path':<14}{'connect s':>10}{'manual s':>10}{'extra s':>9}")
    for r in data:
        print(f"{r['company'][:27]:<28}{r['path'][:13]:<14}{r['connect_seconds']:>10.1f}{r['manual_seconds']:>10.1f}{r['extra_seconds']:>9.1f}")
    if data:
        for name, fn in (("median", statistics.median), ("max", max)):
            vals = [fn(r[k] for r in data) for k in ("connect_seconds", "manual_seconds", "extra_seconds")]
            print(f"{name:<42}{vals[0]:>10.1f}{vals[1]:>10.1f}{vals[2]:>9.1f}")
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["company", "path", "connect_seconds", "manual_seconds", "extra_seconds"])
            w.writeheader()
            w.writerows(data)


if __name__ == "__main__":
    main()
