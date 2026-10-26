"""One table across eval runs: python -m eval.compare results/*.json  (files from listen_eval --json)"""
import json
import sys


def row(path: str) -> list[str]:
    with open(path) as f:
        s = json.load(f)["summary"]
    per_clip = s["cost_usd"] / s["clips"] if s["cost_usd"] is not None and s["clips"] else None
    return [
        s["model"], str(s["clips"]), f"{s['key_accuracy']:.0%}", f"{s['outcome_accuracy']:.0%}", str(s["false_options"]),
        f"{s['median_seconds']:.2f}", f"{s['median_llm_seconds']:.2f}", "n/a" if per_clip is None else f"{per_clip:.4f}",
    ]


def main(paths: list[str] | None = None) -> None:
    paths = sys.argv[1:] if paths is None else paths
    if not paths:
        sys.exit("usage: python -m eval.compare results/*.json")
    table = [["model", "clips", "key acc", "outcome acc", "false options", "median s", "llm s", "$ per clip"]] + [row(p) for p in paths]
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    for r in table:
        print(r[0].ljust(widths[0]) + "".join(c.rjust(w + 2) for c, w in zip(r[1:], widths[1:])))


if __name__ == "__main__":
    main()
