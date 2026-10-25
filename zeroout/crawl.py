import argparse
import logging
import math
import os
import sqlite3
import time
from typing import Callable

from .budget import Budget, calls_today, can_spend
from .config import Settings, load_settings
from .db import LOCK, connect, now, start_run
from .models import Observation
from .paths import build_path, file_part, press_times
from .probe import TAIL_SECONDS, CallFailed, Probe, twilio_probe
from .segment import duration, tail
from .tree import ensure_root, merge, nano_same_meaning, unexplored

log = logging.getLogger(__name__)
BACKOFF = (2, 4)


class _Stop(Exception):
    pass


def crawl(
    conn: sqlite3.Connection,
    settings: Settings,
    company_id: int,
    probe: Probe,
    listen: Callable[[bytes], Observation | None],
    same_meaning: Callable[[str, str], bool | None],
    budget: Budget = Budget(),
    contributor: str = "crawler",
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    run_id = start_run(conn, company_id, "crawl", contributor)
    ensure_root(conn, company_id)
    with LOCK:
        number = conn.execute("SELECT number FROM companies WHERE id = ?", (company_id,)).fetchone()[0]
    # The budget is per company per day; the run row records only this run's usage.
    day_calls, day_minutes = calls_today(conn, company_id)
    calls, minutes = 0, 0.0

    def place(path: str) -> bytes:
        """One call attempt; returns the listenable clip or raises CallFailed/_Stop."""
        nonlocal calls, minutes
        if (
            day_calls + calls >= budget.max_calls
            or day_minutes + minutes >= budget.max_minutes
            or not can_spend(conn, settings)
        ):
            raise _Stop
        steps = build_path(conn, company_id, path, settings.key_margin)
        calls += 1
        try:
            res = probe(number, steps)
        except CallFailed as e:
            minutes += math.ceil(e.seconds / 60)
            _save_run(conn, run_id, calls, minutes, settings)
            raise
        minutes += math.ceil(res.seconds / 60)  # Twilio bills each started minute
        _save_run(conn, run_id, calls, minutes, settings)
        cut = press_times(steps)[-1] if steps else 0
        clip = tail(res.wav, cut) if res.wav else b""
        if not clip or duration(clip) == 0:
            raise CallFailed("empty audio")
        return clip

    def place_retrying(path: str) -> bytes | None:
        for attempt in range(len(BACKOFF) + 1):
            try:
                return place(path)
            except CallFailed as e:
                log.warning("call to %r failed (attempt %d): %s", path, attempt + 1, e)
                if attempt < len(BACKOFF):
                    sleep(BACKOFF[attempt])
        return None

    def observe(path: str, n: int) -> str | None:
        clip = place_retrying(path)
        if clip is None:
            return None
        try:
            obs = listen(clip)
        except Exception:
            log.exception("listen failed for %r", path)
            obs = None
        if obs is None:
            log.warning("unparsed audio for %r", path)
            return None
        file = ""
        if obs.outcome not in ("human", "voicemail"):  # never publish audio of real people
            d = os.path.join(settings.audio_dir, str(company_id))
            os.makedirs(d, exist_ok=True)
            file = os.path.join(d, f"{run_id}-{file_part(path)}-{n}.wav")
            with open(file, "wb") as f:
                f.write(clip)
        try:
            return merge(conn, company_id, path, obs, run_id, file, same_meaning)
        except Exception:
            log.exception("merge failed for %r", path)
            return None

    attempted: set[str] = set()
    try:
        while True:
            path = next((p for p in unexplored(conn, company_id, budget.max_depth) if p not in attempted), None)
            if path is None:
                break
            attempted.add(path)
            if observe(path, 1) == "retry":
                observe(path, 2)
    except _Stop:
        log.info("crawl %d stopped by budget", run_id)
    with LOCK:
        conn.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (now(), run_id))
        conn.commit()
    return run_id


def _save_run(conn: sqlite3.Connection, run_id: int, calls: int, minutes: float, settings: Settings) -> None:
    with LOCK:
        conn.execute(
            "UPDATE runs SET calls = ?, minutes = ?, cost_usd = ? WHERE id = ?",
            (calls, minutes, minutes * settings.rate_per_min, run_id),
        )
        conn.commit()


def main(argv: list[str] | None = None) -> None:
    from . import hear
    from .listen import listen as real_listen

    ap = argparse.ArgumentParser()
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--max-calls", type=int, default=Budget().max_calls)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    s = load_settings()
    try:
        hear.load(s.asr_model_dir)  # before the first paid call
    except RuntimeError as e:
        raise SystemExit(str(e))
    c = connect(s.db_path)
    rid = crawl(
        c, s, args.company_id, twilio_probe(s), lambda w: real_listen(s, w, expected=TAIL_SECONDS),
        nano_same_meaning(s), Budget(max_calls=args.max_calls),
    )
    print(f"run {rid} done")


if __name__ == "__main__":
    main()
