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
from .paths import is_toll_free, normalize_number
from .tree import tree_json

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
BUDGET_MSG = "Daily call budget reached, try again tomorrow"
TOLL_FREE_MSG = "Only US toll-free numbers (800, 888, 877, 866, 855, 844, 833)"
PATH_RE = re.compile(r"[0-9*#](\.[0-9*#])*")
HOUR = 3600
STATIC = Path(__file__).parent / "static"
MIN_SECRET = 32
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
PHONE_MOVED_ON = " Zero-Out moved on from that branch after two tries."
PHONE_DISPUTED = " Zero-Out moved on; an automatic call can check that branch later."
PHONE_PENDING_SECONDS = 600
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
        self.settings, self._client = settings, client

    def _svc(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(self.settings.twilio_sid, self.settings.twilio_token)
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
    connect=None, verify=None, local: LocalGuard | None = None,
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
    app.state.running = set()
    app.state.verify_daily = {"day": "", "count": 0}
    lock = threading.Lock()
    ask_limit, connect_limit, verify_limit = RateLimiter(20), RateLimiter(3), RateLimiter(5)
    phone_limit, crawl_limit, company_limit = RateLimiter(3), RateLimiter(3), RateLimiter(5)
    clip_limit = RateLimiter(PHONE_CLIPS_PER_HOUR)
    # Phone mode state lives here, keyed by (launch, company), never in the cookie.
    ph: dict = {"runs": {}, "skip": {}, "fails": {}, "pending": {}, "busy": set()}
    app.state.phone = ph
    lazy: dict[str, Callable] = {}

    def get_probe():
        if probe is not None:
            return probe
        if "probe" not in lazy:
            from .probe import twilio_probe
            lazy["probe"] = twilio_probe(settings)
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

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("budget_ok", can_spend(conn, settings))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, msg: str, status: int) -> HTMLResponse:
        return render(request, "index.html", status, error=msg, companies=_companies())

    def _companies():
        with db.LOCK:
            return conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM nodes n WHERE n.company_id = c.id "
                "AND n.status != 'unexplored') AS mapped FROM companies c ORDER BY c.name"
            ).fetchall()

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # htmx 1.9 does not swap 4xx responses, so show these errors as 200 partials.
        path = request.url.path
        htmx = request.headers.get("hx-request") == "true" and (
            path.startswith("/verify/") or (path.startswith("/c/") and path.endswith("/ask")))
        return HTMLResponse(f"<p>{_esc(str(exc.detail))}</p>", status_code=200 if htmx else exc.status_code)

    @app.get("/expired", response_class=HTMLResponse)
    def expired():
        if local is None:
            raise HTTPException(404, "Not found")
        return HTMLResponse(f"<p>{EXPIRED_MSG}</p>")

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
                          "options": [{"key": x.key, "label": x.label} for x in o.options]}})
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
                "message": "Skipped that branch.",
                "next": step_json(phone.next_step(conn, cid, path, skip=skip_set(key)))}))
        except HTTPException as e:
            return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    @app.post("/c/{cid}/ask", response_class=HTMLResponse)
    def ask_route(request: Request, cid: int, question: str = Form("")):
        same_origin(request)
        company(cid)
        limited(ask_limit, request)
        ans = ask_question(conn, settings, cid, question[:MAX_QUESTION], chat=get_chat())
        return render(request, "_answer.html", answer=ans, keys=" → ".join(ans.path.split(".")) if ans.path else "")

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
        return HTMLResponse("<p>Code sent. Enter it below.</p>")

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
        return HTMLResponse("<p>Phone verified.</p>")

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
        return HTMLResponse("<p>Calling you now. Pick up to be connected.</p>")

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


def _esc(s: str) -> str:
    from html import escape
    return escape(s)


def __getattr__(name: str):
    if name == "app":
        a = create_app(load_settings())
        globals()["app"] = a
        return a
    raise AttributeError(name)
