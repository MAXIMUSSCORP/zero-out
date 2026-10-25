import base64
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from twilio.base.exceptions import TwilioException

from .config import Settings
from .models import Step
from .paths import effective_wait, is_toll_free

POLL_SECONDS = 3
CALL_TIMEOUT = 120
RECORDING_TIMEOUT = 30
FAILED = {"busy", "failed", "no-answer", "canceled"}
TAIL_SECONDS = 40
HANGUP_SLACK = 30
MAX_RECORDING_BYTES = 64 * 1024 * 1024


@dataclass
class ProbeResult:
    wav: bytes
    seconds: float


class CallFailed(Exception):
    def __init__(self, msg: str = "", seconds: float = 0):
        super().__init__(msg)
        self.seconds = seconds  # billable time the failed call still used


Probe = Callable[[str, list[Step]], ProbeResult]


def steps_twiml(steps: list[Step]) -> str:
    out = []
    for s in steps:
        whole = math.floor(s.wait)
        halves = round((effective_wait(s.wait) - whole) * 2)
        if whole > 0:
            out.append(f'<Pause length="{whole}"/>')
        out.append(f'<Play digits="{"w" * halves}{s.key}"/>')
    return "".join(out)


def twiml_for(steps: list[Step], tail_seconds: int = TAIL_SECONDS) -> str:
    return f'<Response>{steps_twiml(steps)}<Pause length="{tail_seconds}"/><Hangup/></Response>'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _read_capped(r) -> bytes:
    data = r.read(MAX_RECORDING_BYTES + 1)
    if len(data) > MAX_RECORDING_BYTES:
        raise OSError(f"recording is larger than {MAX_RECORDING_BYTES // (1024 * 1024)} MB")
    return data


def _download(url: str, sid: str, token: str) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with opener.open(req, timeout=60) as r:
            return _read_capped(r)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not loc:
            raise
    target = urllib.parse.urljoin(url, loc)
    if urllib.parse.urlparse(target).scheme != "https":
        raise OSError("refusing redirect to a URL that is not https")
    # Followed once, without credentials, so the token never leaves the original host.
    with opener.open(urllib.request.Request(target), timeout=60) as r:
        return _read_capped(r)


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
        if not is_toll_free(number):
            raise ValueError("only toll-free numbers can be called")
        total_wait = sum(effective_wait(s.wait) for s in steps)
        time_limit = int(total_wait + TAIL_SECONDS + HANGUP_SLACK)
        polls_max = -(-max(CALL_TIMEOUT, time_limit) // POLL_SECONDS)
        try:
            call = client.calls.create(
                to=number, from_=settings.twilio_from, twiml=twiml_for(steps), record=True,
                time_limit=time_limit,
            )
            sid = call.sid
            info = None
            for polls in range(polls_max):
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
                raise CallFailed("call timed out", seconds=polls_max * POLL_SECONDS)

            billed = float(info.duration or 0)
            for _ in range(RECORDING_TIMEOUT // POLL_SECONDS):
                recs = client.recordings.list(call_sid=sid, limit=1)
                if recs:
                    rec_sid = recs[0].sid
                    url = (
                        f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_sid}"
                        f"/Recordings/{rec_sid}.wav"
                    )
                    try:
                        wav = download(url, settings.twilio_sid, settings.twilio_token)
                    except Exception:
                        wav = b""
                    if wav:
                        try:
                            client.recordings(rec_sid).delete()
                        except Exception:
                            pass
                        return ProbeResult(wav, billed)
                sleep(POLL_SECONDS)
            raise CallFailed("recording missing", seconds=billed)
        except (TwilioException, OSError) as e:
            raise CallFailed(f"twilio error: {type(e).__name__}") from e

    return probe
