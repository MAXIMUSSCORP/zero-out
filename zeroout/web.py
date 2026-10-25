import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from twilio.base.exceptions import TwilioRestException

from . import db
from .ask import MAX_QUESTION, ask as ask_question
from .budget import calls_today, can_spend
from .config import Settings, load_settings
from .crawl import crawl
from .menu_text import menu_lines
from .paths import is_toll_free, normalize_number
from .settings_page import CHECKS_PER_HOUR, READS_PER_HOUR, SettingsPage
from .tree import tree_json

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
BUDGET_MSG = "Daily call budget reached, try again tomorrow"
TOLL_FREE_MSG = "Only US toll-free numbers (800, 888, 877, 866, 855, 844, 833)"
PATH_RE = re.compile(r"[0-9*#](\.[0-9*#])*")
HOUR = 3600
STATIC = Path(__file__).parent / "static"
MIN_SECRET = 32
CROSS_SITE_MSG = "Settings can only be opened from another page of this Zero-Out, not from another site."
VERIFY_DAILY_CAP = 100
CRAWL_CALLS_PER_DAY = 40
CRAWL_MINUTES_PER_DAY = 30
CRAWL_USED_MSG = "This company was already mapped as much as allowed today"
US_ONLY_MSG = "Only US phone numbers are supported"
PRIVATE_OUTCOMES = ("human", "voicemail")
SWEEP_EVERY = 1000
PHONE_MAX_BYTES = 8 * 1024 * 1024
PHONE_CLIPS_PER_HOUR = 240
PHONE_UNREADABLE_MSG = "The recording could not be read. Try again."
PHONE_FAILED_MSG = "Something went wrong hearing that clip."
PHONE_KEEP_FAILED_MSG = "Something went wrong keeping that clip."
PHONE_NO_MODEL_MSG = "The speech recognition model is not installed on this computer."
PHONE_BUSY_MSG = "Still hearing the last clip."
PHONE_CRAWLING_MSG = "Already mapping this company with automatic calls."
PHONE_GONE_MSG = "That clip is no longer waiting. Record it again."
PHONE_CHECK_MSG = "Check what was heard, then keep it or discard it."
PHONE_MOVED_ON = " Zero-Out moved on from that option after two tries."
PHONE_DISPUTED = " Zero-Out moved on; an automatic call can check that option later."
PHONE_PENDING_SECONDS = 600
CONTRIBUTE_PER_HOUR = 10
UPDATE_PER_HOUR = 6
PREVIEW_SECONDS = 15 * 60
PREVIEW_GONE_MSG = "That preview is no longer waiting. Preview again before sharing."
PREVIEW_CHANGED_MSG = "Something new was mapped since the preview; check this version."
COMMUNITY_BUSY_MSG = "Already mapping this company, try again when it finishes."
SHARE_FAILED_MSG = "Something went wrong sharing that file. Nothing was marked as shared."
UPDATE_FAILED_MSG = "Something went wrong updating from the community. Try again later."
NEEDS_TWILIO_LOCAL_MSG = "Needs Twilio. Add it in Settings."
NEEDS_TWILIO_MSG = "Needs Twilio."
PHONE_TOO_BIG_MSG = "That recording is too long. Stop sooner and try again."
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; media-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
)
# NANP area codes outside the US (Canada, the Caribbean) plus premium-rate 900/976.
NON_US_AREA_CODES = frozenset(
    "204 226 236 249 250 257 263 289 306 343 354 365 367 368 382 403 416 418 431 437 438 450 468 474 "
    "506 514 519 548 579 581 584 587 604 613 639 647 672 683 705 709 742 753 778 780 782 807 819 825 "
    "867 873 879 902 905 "
    "242 246 264 268 284 345 441 473 649 658 664 721 758 767 784 787 809 829 849 868 869 876 939 "
    "670 671 684 340 "
    "900 976".split()
)


class RateLimiter:
    def __init__(self, limit: int, window: float = HOUR, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()
        self.calls = 0

    def _prune(self, key: str, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            self.hits.pop(key, None)

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self.lock:
            self.calls += 1
            if self.calls % SWEEP_EVERY == 0:
                for k, old in list(self.hits.items()):
                    self._prune(k, old, now)
            q = self.hits.get(key)
            if q is not None:
                self._prune(key, q, now)
            if q is None or not q:
                q = self.hits[key] = deque()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class TwilioVerify:
    def __init__(self, settings: Settings, client=None):
        self.settings, self._client, self._injected = settings, client, client is not None
        self._creds = None

    def _svc(self):
        creds = (self.settings.twilio_sid, self.settings.twilio_token)
        # Settings can change the credentials while the app runs, so a client built here is rebuilt then.
        if not self._injected and (self._client is None or creds != self._creds):
            from twilio.rest import Client
            self._client, self._creds = Client(*creds), creds
        return self._client.verify.v2.services(self.settings.twilio_verify_sid)

    def start(self, e164: str) -> None:
        self._svc().verifications.create(to=e164, channel="sms")

    def check(self, e164: str, code: str) -> bool:
        return self._svc().verification_checks.create(to=e164, code=code).status == "approved"


def _outcome(raw_json: str | None) -> str | None:
    try:
        data = json.loads(raw_json or "")
    except ValueError:
        return None
    return data.get("outcome") if isinstance(data, dict) else None


@dataclass(frozen=True)
class LocalGuard:
    """Extra checks for `zeroout run`: this app is for the one person at the keyboard."""
    port: int
    token: str

    @property
    def launch(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()

    @property
    def origins(self) -> frozenset[str]:
        # Browsers leave :80 out of the Origin header.
        ports = ("", f":{self.port}") if self.port == 80 else (f":{self.port}",)
        return frozenset(f"http://{h}{p}" for h in LOCAL_HOSTS for p in ports)


ERROR_HEADINGS = {
    400: "Check that again", 403: "Not allowed", 404: "Not found", 409: "Already in progress",
    429: "Too many requests", 502: "Could not reach the service", 503: "Not available right now",
}
LOCAL_HOSTS = ("127.0.0.1", "localhost")
LOCAL_MAX_AGE = 12 * HOUR
LOCKED_MSG = "Open Zero-Out from the link that zeroout run printed."
EXPIRED_MSG = (
    "This Zero-Out session has ended. Open it again from the link that zeroout run printed, "
    "or restart zeroout run.")


def format_owner(e164: str) -> str:
    if re.fullmatch(r"\+1\d{10}", e164):
        return f"+1 ({e164[2:5]}) {e164[5:8]}-{e164[8:]}"
    return e164


def _annotate(conn, cid: int, node: dict) -> None:
    latest = {}
    with db.LOCK:
        rows = conn.execute(
            "SELECT id, path, raw_json FROM observations "
            "WHERE company_id = ? AND audio_url IS NOT NULL ORDER BY id", (cid,)).fetchall()
    for r in rows:
        # Recordings of real people are never linked; the newest observation decides.
        latest[r["path"]] = None if _outcome(r["raw_json"]) in PRIVATE_OUTCOMES else r["id"]

    def walk(n: dict) -> None:
        n["obs_id"] = latest.get(n["path"])
        for c in n["children"]:
            walk(c)

    walk(node)


def create_app(
    settings: Settings, conn=None, probe=None, listen=None, same=None, chat=None,
    connect=None, verify=None, local: LocalGuard | None = None, services=None,
) -> FastAPI:
    if len(settings.session_secret or "") < MIN_SECRET or settings.session_secret == "change-me":
        raise RuntimeError(
            "SESSION_SECRET must be at least 32 characters; generate one with "
            "python -c 'import secrets;print(secrets.token_urlsafe(48))'")
    if settings.owner_number and local is None:
        raise RuntimeError(
            "OWNER_NUMBER is only for zeroout run on your own computer; "
            "remove it to run a hosted server.")
    conn = conn if conn is not None else db.connect(settings.db_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if local is not None:
        # Added before SessionMiddleware so it runs inside it and can read the session.
        async def local_guard(request: Request, call_next):
            path = request.url.path
            if request.method == "POST" and request.headers.get("origin") not in local.origins:
                return HTMLResponse("<p>Cross-site request refused</p>", status_code=403)
            if path not in ("/start", "/expired") and not path.startswith("/static/"):
                have = str(request.session.get("launch", ""))
                if not hmac.compare_digest(have.encode(), local.launch.encode()):
                    # htmx does not swap 4xx bodies, so send it to a page that explains.
                    hdr = {"HX-Redirect": "/expired"} if request.headers.get("hx-request") else {}
                    return HTMLResponse(f"<p>{LOCKED_MSG}</p>", status_code=403, headers=hdr)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=local_guard)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret,
        https_only=settings.cookie_secure, same_site="lax",
        max_age=LOCAL_MAX_AGE if local is not None else HOUR,
    )
    if local is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if local is not None:
            response.headers["Permissions-Policy"] = "microphone=(self)"
        return response

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["menu_lines"] = menu_lines
    templates.env.filters["when"] = when
    app.state.running = set()
    app.state.verify_daily = {"day": "", "count": 0}
    lock = threading.Lock()
    ask_limit, connect_limit, verify_limit = RateLimiter(20), RateLimiter(3), RateLimiter(5)
    phone_limit, crawl_limit, company_limit = RateLimiter(3), RateLimiter(3), RateLimiter(5)
    clip_limit = RateLimiter(PHONE_CLIPS_PER_HOUR)
    share_limit, update_limit = RateLimiter(CONTRIBUTE_PER_HOUR), RateLimiter(UPDATE_PER_HOUR)
    community = {"sharing": set(), "updating": False, "previews": {}}
    # Phone mode state lives here, keyed by (launch, company), never in the cookie.
    ph: dict = {"runs": {}, "skip": {}, "fails": {}, "pending": {}, "busy": set()}
    app.state.phone = ph
    lazy: dict[str, Callable] = {}

    def get_probe():
        if probe is not None:
            return probe
        creds = (settings.twilio_sid, settings.twilio_token)
        if lazy.get("probe_creds") != creds:  # Settings can change them while the app runs
            from .probe import twilio_probe
            lazy["probe"], lazy["probe_creds"] = twilio_probe(settings), creds
        return lazy["probe"]

    def get_listen():
        if listen is not None:
            return listen
        from .listen import listen as _listen
        from .probe import TAIL_SECONDS
        return lambda w: _listen(settings, w, expected=TAIL_SECONDS)

    def get_same():
        if same is not None:
            return same
        if "same" not in lazy:
            from .tree import nano_same_meaning
            lazy["same"] = nano_same_meaning(settings)
        return lazy["same"]

    def get_chat():
        if chat is not None:
            return chat
        from .llm import chat_json
        return chat_json

    def get_connect():
        if connect is not None:
            return connect
        from .connect import start_connect
        return start_connect

    def get_verify():
        if verify is not None:
            return verify
        if "verify" not in lazy:
            lazy["verify"] = TwilioVerify(settings)
        return lazy["verify"]

    def discard(cid: int) -> None:
        with lock:
            app.state.running.discard(cid)

    def claim(cid: int) -> None:
        with lock:
            if cid in app.state.running:
                raise HTTPException(409, "Already mapping this company")
            app.state.running.add(cid)

    def company(cid: int):
        with db.LOCK:
            row = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown company")
        return row

    def ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def limited(limiter: RateLimiter, request: Request, key: str | None = None) -> None:
        if not limiter.allow(key or ip(request)):
            raise HTTPException(429, "Too many requests, try again later")

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "Cross-site request refused")

    def verify_cap() -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with lock:
            d = app.state.verify_daily
            if d["day"] != day:
                d["day"], d["count"] = day, 0
            if d["count"] >= VERIFY_DAILY_CAP:
                raise HTTPException(429, "Too many codes sent today, try again tomorrow")
            d["count"] += 1

    def twilio_ready() -> bool:
        """Read at call time, since Settings changes the running keys. An injected probe needs none."""
        return probe is not None or all((settings.twilio_sid, settings.twilio_token, settings.twilio_from))

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("budget_ok", can_spend(conn, settings))
        ctx.setdefault("local", local is not None)
        if name == "index.html":
            ctx.setdefault("offers", offered())
            ctx.setdefault("tf_key_set", bool(settings.tf_api_key))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, msg: str, status: int) -> HTMLResponse:
        return render(request, "index.html", status, error=msg, companies=_companies())

    def _companies():
        with db.LOCK:
            return conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM nodes n WHERE n.company_id = c.id "
                "AND n.status != 'unexplored') AS mapped FROM companies c ORDER BY c.name"
            ).fetchall()

    def message_page(request: Request, heading: str, message: str, status: int = 200,
                     cid: int | None = None, note: str = "") -> HTMLResponse:
        """A small full page for a result the person reached by a plain form post."""
        with db.LOCK:
            known = cid is not None and conn.execute(
                "SELECT 1 FROM companies WHERE id = ?", (cid,)).fetchone() is not None
        back = (f"/c/{cid}", "Back to this company") if known else ("/", "Companies")
        return render(request, "message.html", status, heading=heading, message=message,
                      back_href=back[0], back_label=back[1], note=note)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # htmx 1.9 does not swap 4xx responses, so show these errors as 200 partials.
        path = request.url.path
        htmx = request.headers.get("hx-request") == "true" and (
            path.startswith(("/verify/", "/community/"))
            or (local is not None and path.startswith("/settings"))
            or (path.startswith("/c/") and path.endswith(("/ask", "/contribute", "/contribute/preview"))))
        if request.headers.get("hx-request") == "true":
            return HTMLResponse(f'<p class="error-text">{_esc(str(exc.detail))}</p>', status_code=200 if htmx else exc.status_code)
        m = re.match(r"/c/(\d{1,18})(/|$)", path)
        return message_page(
            request, ERROR_HEADINGS.get(exc.status_code, "Something went wrong"), str(exc.detail),
            exc.status_code, cid=int(m.group(1)) if m else None,
            note="Nothing was changed by this request. Go back and try again, or pick another company.")

    @app.get("/expired", response_class=HTMLResponse)
    def expired(request: Request):
        if local is None:
            raise HTTPException(404, "Not found")
        return render(request, "expired.html", message=EXPIRED_MSG, local=False)

    @app.get("/start")
    def start(request: Request, t: str = ""):
        if local is None:
            raise HTTPException(404, "Not found")
        if not hmac.compare_digest(t.encode(), local.token.encode()):
            raise HTTPException(403, LOCKED_MSG)
        request.session["launch"] = local.launch
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html", companies=_companies(), error=None)

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def company_page(request: Request, cid: int):
        c = company(cid)
        with db.LOCK:
            tree = tree_json(conn, cid)
            if tree:
                _annotate(conn, cid, tree)
            changes = conn.execute(
                "SELECT path, created_at FROM observations WHERE company_id = ? AND result = 'changed' "
                "ORDER BY created_at DESC, id DESC", (cid,)).fetchall()
        return render(
            request, "company.html", company=c, tree=tree, changes=changes,
            verified=bool(request.session.get("phone")),
            owner=format_owner(settings.owner_number) if settings.owner_number else "",
            needs_twilio=not settings.owner_number and not settings.twilio_verify_sid,
            running=cid in app.state.running, local=local is not None,
            crawl_ready=twilio_ready(),
        )

    @app.get("/c/{cid}/phone", response_class=HTMLResponse)
    def phone_page(request: Request, cid: int):
        if local is None:
            raise HTTPException(404, "Not found")
        from . import phone
        c = company(cid)
        skipped = frozenset(ph["skip"].get((local.launch, cid), ()))
        return render(request, "phone.html", company=c, skipped=len(skipped),
                      step=phone.next_step(conn, cid, None, skip=skipped))

    def step_json(s) -> dict | None:
        if s is None:
            return None
        return {"path": s.path, "keys": list(s.keys), "press_now": s.press_now, "label": s.label}

    def phone_refuse_crawl(cid: int) -> None:
        if cid in app.state.running:
            raise HTTPException(409, PHONE_CRAWLING_MSG)

    def unusable(key: tuple, path: str) -> bool:
        """Counts a path that came back unusable; the second time it is skipped for good."""
        with lock:
            fails = ph["fails"].setdefault(key, {})
            fails[path] = fails.get(path, 0) + 1
            if fails[path] < 2 or path == "":  # the root is never given up on, or nothing is left to try
                return False
            ph["skip"].setdefault(key, set()).add(path)
            return True

    def with_skipped(key: tuple, body: dict) -> dict:
        body["skipped"] = len(skip_set(key))
        return body

    def skip_set(key: tuple) -> frozenset:
        with lock:
            return frozenset(ph["skip"].get(key, ()))

    def phone_run_id(key: tuple, cid: int, phone) -> int:
        with lock:
            rid = ph["runs"].get(key)
        if rid is not None:
            with db.LOCK:
                alive = conn.execute(
                    "SELECT 1 FROM runs WHERE id = ? AND company_id = ?", (rid, cid)).fetchone()
            if alive is None:
                rid = None
        if rid is None:
            rid = phone.start_phone_run(conn, cid)
            with lock:
                ph["runs"][key] = rid
        return rid

    async def read_raw(request: Request) -> bytes:
        """Reads the raw body under a hard size cap, even when Content-Length is missing or wrong."""
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > PHONE_MAX_BYTES:
            raise HTTPException(413, PHONE_TOO_BIG_MSG)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > PHONE_MAX_BYTES:
                raise HTTPException(413, PHONE_TOO_BIG_MSG)
        return bytes(body)

    async def phone_json(request: Request) -> dict:
        try:
            data = json.loads((await read_raw(request))[:4096] or b"null")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise HTTPException(400, "That request could not be read.")
        return data

    def phone_entry(cid: int, nonce) -> tuple:
        if not isinstance(nonce, str):
            raise HTTPException(400, "That request could not be read.")
        with lock:
            e = ph["pending"].get(nonce)
            if e is None or e[0] != local.launch or e[1] != cid or e[4] < time.monotonic():
                raise HTTPException(404, PHONE_GONE_MSG)
            del ph["pending"][nonce]
        return e

    def phone_guard(cid: int) -> None:
        if local is None:
            raise HTTPException(404, "Not found")
        company(cid)

    @app.post("/c/{cid}/phone/clip")
    async def phone_clip(request: Request, cid: int, path: str = "", capped: str = "0", sound: str = "0"):
        if local is None:
            raise HTTPException(404, "Not found")
        key = (local.launch, cid)
        claimed = False
        try:
            same_origin(request)
            company(cid)
            if path and not PATH_RE.fullmatch(path):
                raise HTTPException(400, "That menu path is not valid.")
            if request.headers.get("content-type", "").split(";")[0].strip().lower() not in ("audio/wav", "audio/x-wav"):
                raise HTTPException(415, "The recording must be sent as audio/wav.")
            limited(clip_limit, request)
            phone_refuse_crawl(cid)
            if listen is None:
                from . import hear
                try:
                    await run_in_threadpool(hear.load, settings.asr_model_dir)
                except RuntimeError as e:
                    log.warning("speech model check failed: %s", e)
                    raise HTTPException(503, PHONE_NO_MODEL_MSG)
            with lock:
                if cid in ph["busy"]:
                    raise HTTPException(409, PHONE_BUSY_MSG)
                ph["busy"].add(cid)
                claimed = True
            wav = await read_raw(request)
            from . import phone
            try:
                heard = await run_in_threadpool(
                    phone.hear_clip, settings, wav, capped=capped == "1", sound=sound == "1")
            except ValueError:
                raise HTTPException(400, PHONE_UNREADABLE_MSG)
            except RuntimeError as e:
                log.warning("phone clip: %s", e)
                raise HTTPException(503, PHONE_NO_MODEL_MSG)
            except Exception:
                log.exception("phone clip failed")
                raise HTTPException(500, PHONE_FAILED_MSG)
            if heard.obs is None:
                moved = unusable(key, path)
                nxt = (phone.next_step(conn, cid, path, skip=skip_set(key)) if moved
                       else phone.retry_step(path))
                return JSONResponse(with_skipped(key, {
                    "message": heard.message + (PHONE_MOVED_ON if moved else ""),
                    "heard": None, "next": step_json(nxt)}))
            now = time.monotonic()
            nonce = secrets.token_urlsafe(16)
            with lock:
                for k in [k for k, e in ph["pending"].items() if e[4] < now]:
                    del ph["pending"][k]
                ph["pending"][nonce] = (local.launch, cid, path, heard.obs, now + PHONE_PENDING_SECONDS)
            o = heard.obs
            return JSONResponse({
                "message": PHONE_CHECK_MSG, "nonce": nonce,
                "heard": {"outcome": o.outcome, "prompt_text": o.prompt_text,
                          "options": [{"key": x.key, "label": x.label} for x in o.options],
                          "lines": menu_lines(o.prompt_text, [x.key for x in o.options])}})
        except HTTPException as e:
            return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
        finally:
            if claimed:
                with lock:
                    ph["busy"].discard(cid)

    @app.post("/c/{cid}/phone/keep")
    async def phone_keep(request: Request, cid: int):
        try:
            phone_guard(cid)
            same_origin(request)
            data = await phone_json(request)
            phone_refuse_crawl(cid)
            _, _, path, obs, _ = phone_entry(cid, data.get("nonce"))
            from . import phone
            key = (local.launch, cid)

            def work():
                rid = phone_run_id(key, cid, phone)
                return phone.keep_clip(conn, cid, path, obs, run_id=rid, same_meaning=get_same())

            try:
                kept = await run_in_threadpool(work)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception:
                log.exception("phone keep failed")
                raise HTTPException(500, PHONE_KEEP_FAILED_MSG)
            moved = disputed = False
            if kept.result == "retry":
                moved = unusable(key, path)
            elif kept.result == "disputed" and path != "":
                # The earlier version is kept, so asking again would offer the same branch forever.
                with lock:
                    ph["skip"].setdefault(key, set()).add(path)
                disputed = True
            else:
                with lock:
                    ph["fails"].get(key, {}).pop(path, None)
            if kept.result == "retry" and not moved:
                nxt = phone.retry_step(path)
            else:
                nxt = phone.next_step(conn, cid, path, skip=skip_set(key))
            return JSONResponse(with_skipped(key, {
                "outcome": kept.outcome,
                "options": [{"key": o.key, "label": o.label} for o in kept.options],
                "result": kept.result,
                "message": kept.message + (PHONE_MOVED_ON if moved else PHONE_DISPUTED if disputed else ""),
                "next": step_json(nxt)}))
        except HTTPException as e:
            return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    @app.post("/c/{cid}/phone/discard")
    async def phone_discard(request: Request, cid: int):
        try:
            phone_guard(cid)
            same_origin(request)
            data = await phone_json(request)
            _, _, path, _, _ = phone_entry(cid, data.get("nonce"))
            from . import phone
            return JSONResponse(with_skipped((local.launch, cid), {
                "message": "Discarded. Nothing was kept.", "next": step_json(phone.retry_step(path))}))
        except HTTPException as e:
            return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    @app.post("/c/{cid}/phone/skip")
    async def phone_skip(request: Request, cid: int):
        try:
            phone_guard(cid)
            same_origin(request)
            data = await phone_json(request)
            path = data.get("path")
            if not isinstance(path, str) or not PATH_RE.fullmatch(path):
                raise HTTPException(400, "That menu path is not valid.")
            from . import phone
            key = (local.launch, cid)
            with lock:
                ph["skip"].setdefault(key, set()).add(path)
            return JSONResponse(with_skipped(key, {
                "message": "Skipped that option.",
                "next": step_json(phone.next_step(conn, cid, path, skip=skip_set(key)))}))
        except HTTPException as e:
            return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    def offered() -> list[dict]:
        if local is None:
            return []
        try:
            from . import community as comm
            return list(comm.list_offers(conn, limit=50))
        except Exception:
            log.exception("listing offers failed")
            return []

    def community_guard(cid: int | None = None) -> None:
        if local is None:
            raise HTTPException(404, "Not found")
        if cid is not None:
            company(cid)

    def preview_context(cid: int, contribution, login, notice: str = "") -> dict:
        from .github import _sources
        doc = contribution.doc
        number = str(doc.get("number") or contribution.relpath.split("/")[1])
        return dict(kind="preview", cid=cid, count=len(contribution.observation_ids), login=login,
                    repo=settings.community_repo, notice=notice, number=number,
                    company_name=str(doc.get("name") or "Unnamed company"),
                    sources=sorted(_sources(doc).items()),
                    text=contribution.body.decode("utf-8", "replace"))

    def keep_preview(cid: int, contribution, login) -> None:
        with lock:
            community["previews"][(local.launch, cid)] = (contribution, login, time.monotonic() + PREVIEW_SECONDS)

    @app.post("/c/{cid}/contribute/preview", response_class=HTMLResponse)
    def contribute_preview(request: Request, cid: int):
        community_guard(cid)
        same_origin(request)
        from . import community as comm, github
        contribution = comm.build_contribution(conn, cid)
        if contribution is None:
            return render(request, "_community.html", kind="nothing")
        login = github.github_login()
        keep_preview(cid, contribution, login)
        return render(request, "_community.html", **preview_context(cid, contribution, login))

    @app.post("/c/{cid}/contribute", response_class=HTMLResponse)
    def contribute(request: Request, cid: int):
        community_guard(cid)
        same_origin(request)
        limited(share_limit, request)
        key = (local.launch, cid)
        with lock:
            if cid in app.state.running or cid in ph["busy"] or cid in community["sharing"]:
                raise HTTPException(409, COMMUNITY_BUSY_MSG)
            entry = community["previews"].pop(key, None)
            if entry is None or entry[2] < time.monotonic():
                raise HTTPException(409, PREVIEW_GONE_MSG)
            community["sharing"].add(cid)
        try:
            from . import community as comm, github, home
            shown, login, _ = entry
            now = comm.build_contribution(conn, cid)
            if now is None:
                return render(request, "_community.html", kind="nothing")
            if sorted(now.observation_ids) != sorted(shown.observation_ids):
                # Share exactly what was shown, or show the new version and ask again.
                keep_preview(cid, now, login)
                return render(request, "_community.html", **preview_context(cid, now, login, PREVIEW_CHANGED_MSG))
            try:
                sent = github.submit(shown, settings.community_repo, outbox_dir=home.data_dir() / "outbox")
            except Exception:
                log.exception("sharing failed")
                return render(request, "_community.html", kind="failed", message=SHARE_FAILED_MSG)
            if sent.url or sent.outbox or sent.partial:
                comm.mark_contributed(conn, shown.observation_ids, datetime.now(timezone.utc).isoformat())
            url = sent.url if sent.url and sent.url.startswith("https://github.com/") else None
            message = sent.message
            if sent.url is None and sent.outbox is not None:
                message += " It is marked as shared on this computer, since the file now exists for you to send."
            return render(request, "_community.html", kind="shared", url=url, message=message)
        finally:
            with lock:
                community["sharing"].discard(cid)

    @app.post("/community/update", response_class=HTMLResponse)
    def community_update(request: Request):
        community_guard()
        same_origin(request)
        limited(update_limit, request)
        with lock:
            if community["updating"]:
                raise HTTPException(409, "Already updating from the community.")
            community["updating"] = True
        try:
            from . import community as comm, github
            try:
                download = github.download_trees(settings.community_repo)
                report = comm.import_files(conn, download.files, same_meaning=get_same(),
                                           truncated=download.truncated)
            except github.CommunityError as e:
                return render(request, "_community.html", kind="failed", message=str(e))
            except Exception:
                log.exception("update failed")
                return render(request, "_community.html", kind="failed", message=UPDATE_FAILED_MSG)
            return render(request, "_community.html", kind="updated", report=report,
                          invalid=list(report.invalid)[:10], offers=offered(), oob=True)
        finally:
            with lock:
                community["updating"] = False

    @app.post("/c/{cid}/ask", response_class=HTMLResponse)
    def ask_route(request: Request, cid: int, question: str = Form("")):
        same_origin(request)
        company(cid)
        limited(ask_limit, request)
        ans = ask_question(conn, settings, cid, question[:MAX_QUESTION], chat=get_chat())
        return render(request, "_answer.html", answer=ans)

    @app.post("/companies")
    def add_company(request: Request, name: str = Form(""), number: str = Form("")):
        same_origin(request)
        limited(company_limit, request)
        name = name.strip()
        if not name or len(name) > 100:
            return error_page(request, "Enter a company name (up to 100 characters)", 400)
        try:
            e164 = normalize_number(number)
        except ValueError as e:
            return error_page(request, str(e), 400)
        if not is_toll_free(e164):
            return error_page(request, TOLL_FREE_MSG, 400)
        find = "SELECT id FROM companies WHERE number = ?"
        with db.LOCK:
            row = conn.execute(find, (e164,)).fetchone()
            try:
                cid = row["id"] if row else db.add_company(conn, name, e164)
            except sqlite3.IntegrityError:
                cid = conn.execute(find, (e164,)).fetchone()["id"]
        return RedirectResponse(f"/c/{cid}", status_code=303)

    @app.post("/c/{cid}/crawl")
    def crawl_route(request: Request, cid: int, tasks: BackgroundTasks):
        same_origin(request)
        company(cid)
        if not twilio_ready():
            raise HTTPException(400, NEEDS_TWILIO_LOCAL_MSG if local is not None else NEEDS_TWILIO_MSG)
        limited(crawl_limit, request)
        if listen is None:
            from . import hear
            from .listen import NO_MODEL_MSG
            try:
                hear.load(settings.asr_model_dir)  # before the first paid call
            except RuntimeError as e:
                log.warning("speech model check failed: %s", e)
                raise HTTPException(503, NO_MODEL_MSG)
        with lock:
            if cid in app.state.running:
                raise HTTPException(409, "Already mapping this company")
            if not can_spend(conn, settings):
                raise HTTPException(503, BUDGET_MSG)
            calls, minutes = calls_today(conn, cid)
            if calls >= CRAWL_CALLS_PER_DAY or minutes >= CRAWL_MINUTES_PER_DAY:
                raise HTTPException(429, CRAWL_USED_MSG)
            app.state.running.add(cid)

        def run() -> None:
            try:
                crawl(conn, settings, cid, get_probe(), get_listen(), get_same())
            finally:
                discard(cid)

        tasks.add_task(run)
        return RedirectResponse(f"/c/{cid}", status_code=303)

    @app.post("/verify/start")
    def verify_start(request: Request, phone: str = Form("")):
        if settings.owner_number:
            raise HTTPException(404, "Not found")
        same_origin(request)
        try:
            e164 = normalize_number(phone)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if e164[2:5] in NON_US_AREA_CODES:
            raise HTTPException(400, US_ONLY_MSG)
        limited(verify_limit, request)
        limited(phone_limit, request, e164)
        verify_cap()
        try:
            get_verify().start(e164)
        except Exception:
            raise HTTPException(502, "Could not send the code, try again later")
        return HTMLResponse("<p class=\"notice\">Code sent. Enter it below.</p>")

    @app.post("/verify/check")
    def verify_check(request: Request, phone: str = Form(""), code: str = Form("")):
        if settings.owner_number:
            raise HTTPException(404, "Not found")
        same_origin(request)
        try:
            e164 = normalize_number(phone)
        except ValueError as e:
            raise HTTPException(400, str(e))
        limited(verify_limit, request)
        try:
            ok = get_verify().check(e164, code.strip()[:10])
        except Exception:
            raise HTTPException(502, "Could not check the code, try again later")
        if not ok:
            raise HTTPException(400, "Wrong or expired code")
        request.session["phone"] = e164
        return HTMLResponse("<p class=\"notice\">Phone verified.</p>")

    @app.post("/c/{cid}/connect")
    def connect_route(request: Request, cid: int, path: str = Form("")):
        same_origin(request)
        phone = settings.owner_number or request.session.get("phone")
        if not phone:
            raise HTTPException(403, "Verify your phone first")
        company(cid)
        if path and not PATH_RE.fullmatch(path):
            raise HTTPException(400, "Invalid menu path")
        limited(connect_limit, request)
        with lock:
            if cid in app.state.running:
                raise HTTPException(409, "Already mapping this company")
            if not can_spend(conn, settings):
                raise HTTPException(503, BUDGET_MSG)
            app.state.running.add(cid)
        try:
            get_connect()(conn, settings, cid, path, phone, on_done=lambda: discard(cid))
        except ValueError as e:
            discard(cid)
            raise HTTPException(400, str(e))
        except TwilioRestException as e:
            discard(cid)
            log.warning("connect refused by Twilio, code %s", e.code)
            if e.code == 21210:
                raise HTTPException(400, (
                    f"Twilio refused the caller ID: {settings.twilio_from} is not a verified "
                    "caller ID yet. Finish verifying it in the Twilio Console, then try again."))
            raise HTTPException(502, "Twilio could not place the call, try again later")
        except Exception:
            discard(cid)
            raise
        if request.headers.get("hx-request") != "true":
            return message_page(
                request, "Calling you now", "Calling you now. Pick up to be connected.", cid=cid,
                note="Zero-Out dials you first, then the company, and presses the keys you chose. "
                     "Keep your phone nearby until it rings.")
        return HTMLResponse("<p class=\"notice\">Calling you now. Pick up to be connected.</p>")

    # ---------- settings (zeroout run only) ----------

    settings_page = SettingsPage(settings, services)
    settings_limit, settings_reads = RateLimiter(CHECKS_PER_HOUR), RateLimiter(READS_PER_HOUR)

    def settings_guard(request: Request, limit: bool = False, reads: bool = False) -> None:
        if local is None:
            raise HTTPException(404, "Not found")
        if request.method == "GET" and request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, CROSS_SITE_MSG)
        if limit:
            same_origin(request)
            limited(settings_limit, request)
        if reads:
            limited(settings_reads, request)

    def settings_part(request: Request, ctx: dict) -> HTMLResponse:
        return render(request, f"_settings_{ctx['section']}.html", c=ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request):
        settings_guard(request)
        return render(request, "settings.html", p=settings_page.page())

    @app.post("/settings/model", response_class=HTMLResponse)
    def settings_model(request: Request, api_key: str = Form(""), base_url: str = Form(""),
                       nano: str = Form(""), super_: str = Form("", alias="super")):
        settings_guard(request, limit=True)
        form = {"api_key": api_key, "base_url": base_url, "nano": nano, "super": super_}
        return settings_part(request, settings_page.model(form, save=True))

    @app.post("/settings/model/check", response_class=HTMLResponse)
    def settings_model_check(request: Request, api_key: str = Form(""), base_url: str = Form(""),
                             nano: str = Form(""), super_: str = Form("", alias="super")):
        settings_guard(request, limit=True)
        form = {"api_key": api_key, "base_url": base_url, "nano": nano, "super": super_}
        return settings_part(request, settings_page.model(form, save=False))

    @app.post("/settings/phone", response_class=HTMLResponse)
    def settings_phone(request: Request, sid: str = Form(""), token: str = Form(""), number: str = Form("")):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.phone({"sid": sid, "token": token, "number": number}))

    @app.post("/settings/phone/verify", response_class=HTMLResponse)
    def settings_verify(request: Request):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.verify())

    @app.post("/settings/phone/verify/status", response_class=HTMLResponse)
    def settings_verify_status(request: Request):
        settings_guard(request, reads=True)
        same_origin(request)
        return settings_part(request, settings_page.verify_status())

    @app.post("/settings/phone/stop", response_class=HTMLResponse)
    def settings_phone_stop(request: Request):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.stop())

    @app.get("/settings/phone/number", response_class=HTMLResponse)
    def settings_phone_number(request: Request):
        settings_guard(request, reads=True)
        return render(request, "_settings_number.html", c=settings_page.number())

    @app.post("/settings/speech", response_class=HTMLResponse)
    def settings_speech(request: Request, model: str = Form("")):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.speech({"model": model}))

    @app.post("/settings/spending", response_class=HTMLResponse)
    def settings_spending(request: Request, cap: str = Form("")):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.spending({"cap": cap}))

    @app.post("/settings/community", response_class=HTMLResponse)
    def settings_community(request: Request, repo: str = Form("")):
        settings_guard(request, limit=True)
        return settings_part(request, settings_page.community({"repo": repo}))

    @app.get("/settings/community/account", response_class=HTMLResponse)
    def settings_account(request: Request):
        settings_guard(request, reads=True)
        return render(request, "_settings_account.html", c=settings_page.account())

    @app.get("/api/companies/{cid}/tree")
    def api_tree(cid: int):
        company(cid)
        with db.LOCK:
            return tree_json(conn, cid) or {}

    @app.get("/audio/{obs_id}")
    def audio(obs_id: int):
        with db.LOCK:
            row = conn.execute(
                "SELECT audio_url, raw_json FROM observations WHERE id = ?", (obs_id,)).fetchone()
        if row is None or not row["audio_url"] or _outcome(row["raw_json"]) in PRIVATE_OUTCOMES:
            raise HTTPException(404, "No audio")
        base = Path(settings.audio_dir).resolve()
        f = Path(row["audio_url"]).resolve()
        if not f.is_relative_to(base) or not f.is_file():
            raise HTTPException(404, "No audio")
        return FileResponse(f, media_type="audio/wav")

    return app


def when(value) -> str:
    """A stored timestamp as a plain date; anything that does not parse is shown as it is."""
    if not value:
        return ""
    text = str(value)
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return text
    return f"{d:%b} {d.day}, {d.year}"


def _esc(s: str) -> str:
    from html import escape
    return escape(s)


def __getattr__(name: str):
    if name == "app":
        a = create_app(load_settings())
        globals()["app"] = a
        return a
    raise AttributeError(name)
