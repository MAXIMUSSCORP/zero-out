"""Score `listen` against hand-labelled prompts.

eval/labels.json: [{"file": "path/to.wav", "options": [{"key": "1", "label": "billing"}], "outcome": "menu"}]

python -m eval.listen_eval [labels.json] [--audio-root DIR] [--model NAME] [--price IN,OUT] [--json OUT]
--price is USD per million input and output tokens. The app prices no model calls (runs.cost_usd is
Twilio minutes), so without it cost is left null and only tokens are recorded.
Clips go through listen() as the crawler calls it (expected=TAIL_SECONDS), so a silent clip scores as
the app would record it: hold when it runs most of the tail, otherwise hangup. The speech model is
loaded before the first clip, so no clip's seconds include the load.
"""
import argparse
import dataclasses
import json
import os
import re
import statistics
import sys
import time
from types import SimpleNamespace

from zeroout.config import load_settings
from zeroout.listen import listen
from zeroout.models import Observation
from zeroout.probe import TAIL_SECONDS

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


def false_options(gold: dict, pred: Observation | None) -> int:
    """Predicted keys the gold does not have: options a caller would be offered that the menu never made."""
    have = {o["key"] for o in gold["options"]}
    return sum(1 for o in pred.options if o.key not in have) if pred else 0


def resolve(file: str, root: str | None) -> str:
    return os.path.join(root, file) if root and not os.path.isabs(file) else file


class Meter:
    """Stands in for the OpenAI client inside chat_json and totals time and tokens over its calls."""

    def __init__(self, client):
        self.client, self.seconds, self.prompt_tokens, self.completion_tokens = client, 0.0, 0, 0
        self.unknown = False  # a call came back without usage, so the token totals are a lower bound
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        t = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(**kw)
        finally:
            self.seconds += time.perf_counter() - t
        usage = getattr(resp, "usage", None)
        self.unknown = self.unknown or usage is None
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        return resp


def _cost(prompt: int, completion: int, price: tuple[float, float] | None) -> float | None:
    return None if price is None else (prompt * price[0] + completion * price[1]) / 1e6


def run(settings, labels: list[dict], *, audio_root=None, price=None, client=None, transcribe=None, source="") -> dict:
    if client is None:
        import openai
        client = openai.OpenAI(base_url=settings.tf_base_url, api_key=settings.tf_api_key, timeout=60)
    if transcribe is None:
        from zeroout import hear
        hear.load(settings.asr_model_dir)
        transcribe = hear.transcribe
    from zeroout.llm import chat_json
    clips = []
    for item in labels:
        meter, asr = Meter(client), [0.0]

        def timed(wav, model_dir):
            t = time.perf_counter()
            try:
                return transcribe(wav, model_dir)
            finally:
                asr[0] += time.perf_counter() - t

        with open(resolve(item["file"], audio_root), "rb") as f:
            wav = f.read()
        t = time.perf_counter()
        pred = listen(settings, wav, transcribe=timed, expected=TAIL_SECONDS,
                      chat=lambda s, m, msgs, **kw: chat_json(s, m, msgs, client=meter, **kw))
        r, n, ok = score(item, pred)
        clips.append({
            "file": item["file"], "keys_right": r, "keys_total": n, "false_options": false_options(item, pred),
            "outcome_ok": ok, "outcome": pred.outcome if pred else None,
            "seconds": time.perf_counter() - t, "asr_seconds": asr[0], "llm_seconds": meter.seconds,
            "prompt_tokens": None if meter.unknown else meter.prompt_tokens,
            "completion_tokens": None if meter.unknown else meter.completion_tokens,
            "cost_usd": None if meter.unknown else _cost(meter.prompt_tokens, meter.completion_tokens, price),
        })
    return {"summary": summarize(settings.model_nano, clips, price, source), "clips": clips}


def summarize(model: str, clips: list[dict], price=None, source="") -> dict:
    right, total = sum(c["keys_right"] for c in clips), sum(c["keys_total"] for c in clips)
    known = [c for c in clips if c["prompt_tokens"] is not None]
    prompt, completion = sum(c["prompt_tokens"] for c in known), sum(c["completion_tokens"] for c in known)
    unknown = len(clips) - len(known)
    ok = sum(c["outcome_ok"] for c in clips)
    med = lambda key: statistics.median(c[key] for c in clips) if clips else 0.0
    return {
        "model": model, "clips": len(clips),
        "key_accuracy": right / total if total else 0.0, "keys_right": right, "keys_total": total,
        "outcome_accuracy": ok / len(clips) if clips else 0.0, "outcomes_right": ok,
        "false_options": sum(c["false_options"] for c in clips),
        "median_seconds": med("seconds"), "total_seconds": sum(c["seconds"] for c in clips),
        "median_llm_seconds": med("llm_seconds"), "median_asr_seconds": med("asr_seconds"),
        "prompt_tokens": prompt, "completion_tokens": completion, "unknown_usage_clips": unknown,
        "cost_usd": None if unknown else _cost(prompt, completion, price),
        "price_per_million": list(price) if price else None, "labels": source,
    }


def main(argv: list[str] | None = None, *, client=None, transcribe=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m eval.listen_eval")
    ap.add_argument("labels", nargs="?", default="eval/labels.json")
    ap.add_argument("--audio-root", help="directory that relative `file` entries are resolved against")
    ap.add_argument("--model", help="model to use in place of MODEL_NANO")
    ap.add_argument("--price", help="USD per million input,output tokens, for example 0.1,0.4")
    ap.add_argument("--json", metavar="OUT", help="write per-clip results and the summary here")
    args = ap.parse_args(argv)
    with open(args.labels) as f:
        labels = json.load(f)
    if not labels:
        sys.exit(f"No labelled prompts in {args.labels} yet.")
    price = tuple(float(x) for x in args.price.split(",")) if args.price else None
    if price is not None and len(price) != 2:
        sys.exit("--price takes two numbers: input,output USD per million tokens.")
    settings = load_settings()
    if args.model:
        settings = dataclasses.replace(settings, model_nano=args.model)
    out = run(settings, labels, audio_root=args.audio_root, price=price, client=client, transcribe=transcribe,
               source=args.labels)
    for c in out["clips"]:
        print(f"{c['file']}: keys {c['keys_right']}/{c['keys_total']}, outcome {'ok' if c['outcome_ok'] else 'MISS'} ({c['outcome'] or 'unparsed'})")
    s = out["summary"]
    print(f"key_accuracy={s['key_accuracy']:.3f} ({s['keys_right']}/{s['keys_total']})")
    print(f"outcome_accuracy={s['outcome_accuracy']:.3f} ({s['outcomes_right']}/{s['clips']})")
    print(f"false_options={s['false_options']}")
    cost = "n/a" if s["cost_usd"] is None else f"{s['cost_usd']:.4f}"
    print(f"model={s['model']} median_seconds={s['median_seconds']:.2f} (llm {s['median_llm_seconds']:.2f}, asr {s['median_asr_seconds']:.2f})"
          f" tokens={s['prompt_tokens']}+{s['completion_tokens']} cost_usd={cost}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
