"""Per-company coverage and cost: python -m eval.report [--db PATH]"""
import argparse
import sqlite3

from zeroout.config import load_settings
from zeroout.db import connect


def rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for c in conn.execute("SELECT id, name FROM companies ORDER BY name"):
        n = conn.execute(
            "SELECT COUNT(*) AS mapped, COALESCE(SUM(confirmations >= 2), 0) AS confirmed "
            "FROM nodes WHERE company_id = ? AND status != 'unexplored'",
            (c["id"],),
        ).fetchone()
        r = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(minutes), 0) AS minutes "
            "FROM runs WHERE company_id = ? AND kind = 'crawl'",
            (c["id"],),
        ).fetchone()
        out.append({
            "name": c["name"], "mapped": n["mapped"], "cost_usd": r["cost"], "minutes": r["minutes"],
            "confirmation_rate": n["confirmed"] / n["mapped"] if n["mapped"] else 0.0,
        })
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m eval.report")
    ap.add_argument("--db", help="database to read (default: DB_PATH from settings)")
    args = ap.parse_args(argv)
    print(f"{'company':<28}{'mapped':>8}{'crawl $':>10}{'minutes':>10}{'confirmed':>11}")
    for r in rows(connect(args.db or load_settings().db_path)):
        print(f"{r['name'][:27]:<28}{r['mapped']:>8}{r['cost_usd']:>10.2f}{r['minutes']:>10.1f}"
              f"{r['confirmation_rate']:>10.0%}")


if __name__ == "__main__":
    main()
