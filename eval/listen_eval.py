"""Score `listen` against hand-labelled prompts.

eval/labels.json: [{"file": "path/to.wav", "options": [{"key": "1", "label": "billing"}], "outcome": "menu"}]
"""
import json
import re
import sys

from zeroout.config import load_settings
from zeroout.listen import listen
from zeroout.models import Observation

STOP = {"a", "an", "the", "to", "for", "or", "and", "of", "your", "my", "press", "if", "you", "about"}


def _words(label: str) -> set[str]:
    return set(re.findall(r"\w+", label.lower())) - STOP


def label_match(gold: str, pred: str) -> bool:
    g, p = _words(gold), _words(pred)
    return gold.strip().lower() == pred.strip().lower() or bool(g & p)


def score(gold: dict, pred: Observation | None) -> tuple[int, int, bool]:
    """Returns (keys right, keys labelled, outcome right)."""
    labels = {o.key: o.label for o in pred.options} if pred else {}
    right = sum(1 for o in gold["options"] if o["key"] in labels and label_match(o["label"], labels[o["key"]]))
    return right, len(gold["options"]), pred is not None and pred.outcome == gold["outcome"]


def main(path: str = "eval/labels.json") -> None:
    with open(path) as f:
        labels = json.load(f)
    if not labels:
        sys.exit(f"No labelled prompts in {path} yet.")
    settings = load_settings()
    keys_right = keys_total = outcomes_right = 0
    for item in labels:
        with open(item["file"], "rb") as f:
            pred = listen(settings, f.read())
        r, t, ok = score(item, pred)
        keys_right, keys_total, outcomes_right = keys_right + r, keys_total + t, outcomes_right + ok
        print(f"{item['file']}: keys {r}/{t}, outcome {'ok' if ok else 'MISS'} ({pred.outcome if pred else 'unparsed'})")
    print(f"key_accuracy={keys_right / keys_total if keys_total else 0:.3f} ({keys_right}/{keys_total})")
    print(f"outcome_accuracy={outcomes_right / len(labels):.3f} ({outcomes_right}/{len(labels)})")


if __name__ == "__main__":
    main(*sys.argv[1:])
