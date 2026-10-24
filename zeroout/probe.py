import base64
import math
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .config import Settings
from .models import Step

POLL_SECONDS = 3
CALL_TIMEOUT = 120
RECORDING_TIMEOUT = 30
FAILED = {"busy", "failed", "no-answer", "canceled"}


@dataclass
class ProbeResult:
    wav: bytes
    seconds: float


class CallFailed(Exception):
    pass


Probe = Callable[[str, list[Step]], ProbeResult]


def steps_twiml(steps: list[Step]) -> str:
    out = []
    for s in steps:
        whole = math.floor(s.wait)
        halves = math.ceil((s.wait - whole) * 2 - 1e-9)
        if whole > 0:
            out.append(f'<Pause length="{whole}"/>')
        out.append(f'<Play digits="{"w" * halves}{s.key}"/>')
    return "".join(out)


def twiml_for(steps: list[Step], tail_seconds: int = 40) -> str:
    return f'<Response>{steps_twiml(steps)}<Pause length="{tail_seconds}"/><Hangup/></Response>'


def _download(url: str, sid: str, token: str) -> bytes:
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def twilio_probe(
    settings: Settings,
    client=None,
    sleep: Callable[[float], None] = time.sleep,
    download: Callable[[str, str, str], bytes] = _download,
) -> Probe:
    if client is None:
        from twilio.rest import Client
        client = Client(settings.twilio_sid, settings.twilio_token)

    def probe(number: str, steps: list[Step]) -> ProbeResult:
        call = client.calls.create(
            to=number, from_=settings.twilio_from, twiml=twiml_for(steps), record=True
        )
        sid = call.sid
        info = None
        for _ in range(CALL_TIMEOUT // POLL_SECONDS):
            info = client.calls(sid).fetch()
            if info.status in FAILED:
                raise CallFailed(f"call {info.status}")
            if info.status == "completed":
                break
            sleep(POLL_SECONDS)
        else:
            try:
                client.calls(sid).update(status="completed")
            except Exception:
                pass
            raise CallFailed("call timed out")

        for _ in range(RECORDING_TIMEOUT // POLL_SECONDS):
            recs = client.recordings.list(call_sid=sid, limit=1)
            if recs:
                url = (
                    f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_sid}"
                    f"/Recordings/{recs[0].sid}.wav"
                )
                try:
                    wav = download(url, settings.twilio_sid, settings.twilio_token)
                except Exception:
                    wav = b""
                if wav:
                    return ProbeResult(wav, float(info.duration or 0))
            sleep(POLL_SECONDS)
        raise CallFailed("recording missing")

    return probe
