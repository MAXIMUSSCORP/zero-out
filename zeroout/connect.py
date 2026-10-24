import logging
import math
import os
import sqlite3
import threading
import time
from typing import Callable

from .budget import can_spend
from .config import Settings
from .db import LOCK, get_node, now, start_run
from .models import Observation, Step
from .paths import build_path, is_toll_free, press_times
from .probe import FAILED, _download, steps_twiml
from .segment import duration, segment
from .tree import merge, nano_same_meaning

log = logging.getLogger(__name__)
ANSWER_TIMEOUT = 120
USER_TIMEOUT = 90
RECORDING_TIMEOUT = 60
LEAD_SECONDS = 2
CALL_TIME_LIMIT = 1800
STOP_ATTEMPTS = 3
# Both legs keep running after the user joins and we cannot see when they end,
# so every connect is charged this many extra minutes. Deliberately a
# conservative over-estimate so the daily cap stays safe.
RESERVE_MINUTES = 30
Listen = Callable[[bytes], Observation | None]


def ingest_connect_recording(
    conn: sqlite3.Connection,
    settings: Settings,
    company_id: int,
    path: str,
    wav: bytes,
    times: list[float],
    run_id: int,
    listen: Listen,
    same_meaning: Callable[[str, str], bool | None],
) -> list[str]:
    keys = path.split(".") if path else []
    pieces = segment(wav, times)[: len(keys) + 1]
    results = []
    for i, clip in enumerate(pieces):
        prefix = ".".join(keys[:i])
        obs = None
        if clip and duration(clip) > 0:
            try:
                obs = listen(clip)
            except Exception:
                log.exception("listen failed for %r", prefix)
        if obs is None:
            results.append("unparsed")
            continue
        file = ""
        if obs.outcome not in ("human", "voicemail"):  # never keep audio of real people
            d = os.path.join(settings.audio_dir, str(company_id))
            os.makedirs(d, exist_ok=True)
            file = os.path.join(d, f"{run_id}-connect-{prefix or 'root'}.wav")
            with open(file, "wb") as f:
                f.write(clip)
        results.append(merge(conn, company_id, prefix, obs, run_id, file, same_meaning))
    return results


def _save_run(
    conn: sqlite3.Connection, run_id: int, minutes: float, settings: Settings, final: bool = True
) -> None:
    with LOCK:
        conn.execute(
            "UPDATE runs SET calls = 2, minutes = ?, cost_usd = ?, ended_at = ? WHERE id = ?",
            (minutes, minutes * settings.rate_per_min, now() if final else None, run_id),
        )
        conn.commit()


def _stop_recording(client, company_sid: str, rec_sid: str, sleep: Callable[[float], None]) -> bool:
    for attempt in range(STOP_ATTEMPTS):
        try:
            client.calls(company_sid).recordings(rec_sid).update(status="stopped")
            return True
        except Exception:
            log.warning("recording stop attempt %d failed", attempt + 1)
            if attempt < STOP_ATTEMPTS - 1:
                sleep(1)
    return False


def _run_connect(
    conn: sqlite3.Connection,
    settings: Settings,
    run_id: int,
    company_id: int,
    path: str,
    user_e164: str,
    steps: list[Step],
    client,
    company_sid: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    listen: Listen,
    same_meaning: Callable[[str, str], bool | None],
    download: Callable[[str, str, str], bytes] = _download,
    on_done: Callable[[], None] | None = None,
) -> None:
    started = clock()
    rec_sid = user_sid = None
    stopped = clean = False
    try:
        answered = None
        while clock() - started < ANSWER_TIMEOUT:
            status = client.calls(company_sid).fetch().status
            if status == "in-progress":
                answered = clock()
                break
            if status in FAILED or status == "completed":
                break
            sleep(1)
        if answered is None:
            log.warning("connect %d: company call did not connect", run_id)
            return
        # Company side only: inbound on the company leg is audio from the company.
        rec_sid = client.calls(company_sid).recordings.create(recording_track="inbound").sid
        delay = clock() - answered  # the recording starts a little after the answer
        times = press_times(steps)
        sleep(max(times[-1] + LEAD_SECONDS - (clock() - answered), 0) if times else 0)

        user_sid = client.calls.create(
            to=user_e164,
            from_=settings.twilio_from,
            time_limit=CALL_TIME_LIMIT,
            twiml=(
                '<Response><Say>Connecting you now.</Say><Dial>'
                f'<Conference beep="false" endConferenceOnExit="true">zo-{run_id}</Conference>'
                "</Dial></Response>"
            ),
        ).sid
        dialed = clock()
        joined = False
        while clock() - dialed < USER_TIMEOUT:
            status = client.calls(user_sid).fetch().status
            if status == "in-progress":
                joined = True
                break
            if status in FAILED or status == "completed":
                break
            sleep(1)
        stopped = _stop_recording(client, company_sid, rec_sid, sleep)
        if not joined:
            return

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_sid}"
            f"/Recordings/{rec_sid}.wav"
        )
        wav = b""
        waited = 0
        while not wav and waited < RECORDING_TIMEOUT:
            try:
                wav = download(url, settings.twilio_sid, settings.twilio_token)
            except Exception:
                wav = b""
            if not wav:
                sleep(3)
                waited += 3
        if wav:
            try:
                client.recordings(rec_sid).delete()
            except Exception:
                log.warning("connect %d: could not delete recording", run_id)
            try:
                ingest_connect_recording(
                    conn, settings, company_id, path, wav,
                    [max(t - delay, 0) for t in times], run_id, listen, same_meaning,
                )
            except Exception as e:
                log.error("connect %d ingest failed: %s", run_id, type(e).__name__)
        else:
            log.warning("connect %d: recording missing", run_id)
        clean = stopped
    except Exception as e:
        log.error("connect %d failed: %s", run_id, type(e).__name__)
    finally:
        try:
            if rec_sid and not stopped:
                stopped = _stop_recording(client, company_sid, rec_sid, sleep)
            if not stopped and rec_sid:
                clean = False
            if not clean:
                _hangup(client, company_sid)
                if user_sid:
                    _hangup(client, user_sid)
            minutes = math.ceil(max(clock() - started, 0) / 60) + RESERVE_MINUTES
            _save_run(conn, run_id, minutes, settings)
        except Exception as e:
            log.error("connect %d cleanup failed: %s", run_id, type(e).__name__)
        finally:
            if on_done:
                on_done()


def _hangup(client, sid: str) -> None:
    try:
        client.calls(sid).update(status="completed")
    except Exception:
        pass


def start_connect(
    conn: sqlite3.Connection,
    settings: Settings,
    company_id: int,
    path: str,
    user_e164: str,
    *,
    client=None,
    sleep: Callable[[float], None] = time.sleep,
    listen: Listen | None = None,
    same_meaning: Callable[[str, str], bool | None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> int:
    with LOCK:
        row = conn.execute("SELECT number FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None or not is_toll_free(row["number"]):
        raise ValueError("Connect me only calls toll-free numbers")
    node = get_node(conn, company_id, path)
    if node is None or node["status"] == "unexplored":
        raise ValueError("That path has not been mapped yet")
    if not can_spend(conn, settings):
        raise ValueError("Daily budget reached")
    if client is None:
        from twilio.rest import Client
        client = Client(settings.twilio_sid, settings.twilio_token)
    if listen is None:
        from . import listen as listen_mod
        listen = lambda w: listen_mod.listen(settings, w)
    if same_meaning is None:
        same_meaning = nano_same_meaning(settings)

    steps = build_path(conn, company_id, path, settings.key_margin)
    run_id = start_run(conn, company_id, "connect", "connect")
    # Provisional charge so concurrent connects see this spend; replaced when the run ends.
    _save_run(conn, run_id, RESERVE_MINUTES, settings, final=False)
    try:
        call = client.calls.create(
            to=row["number"],
            from_=settings.twilio_from,
            time_limit=CALL_TIME_LIMIT,
            twiml=(
                f"<Response>{steps_twiml(steps)}<Dial>"
                '<Conference startConferenceOnEnter="true" endConferenceOnExit="true" '
                f'beep="false" waitUrl="">zo-{run_id}</Conference></Dial></Response>'
            ),
        )
    except BaseException:
        _save_run(conn, run_id, 0, settings)  # nothing was dialled, so release the reserve
        raise
    threading.Thread(
        target=_run_connect,
        args=(conn, settings, run_id, company_id, path, user_e164, steps, client, call.sid,
              sleep, time.monotonic, listen, same_meaning, _download, on_done),
        daemon=True,
    ).start()
    return run_id
