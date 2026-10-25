import json
import logging
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

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
    connect=None, verify=None,
) -> FastAPI:
    if len(settings.session_secret or "") < MIN_SECRET or settings.session_secret == "change-me":
        raise RuntimeError(
            "SESSION_SECRET must be at least 32 characters; generate one with "
            "python -c 'import secrets;print(secrets.token_urlsafe(48))'")
    conn = conn if conn is not None else db.connect(settings.db_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret,
        https_only=settings.cookie_secure, same_site="lax", max_age=HOUR,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    templates = Jinja2Templates(directory=str(TEMPLATES))
    app.state.running = set()
    app.state.verify_daily = {"day": "", "count": 0}
    lock = threading.Lock()
    ask_limit, connect_limit, verify_limit = RateLimiter(20), RateLimiter(3), RateLimiter(5)
    phone_limit, crawl_limit, company_limit = RateLimiter(3), RateLimiter(3), RateLimiter(5)
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
            verified=bool(request.session.get("phone")), running=cid in app.state.running,
        )

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
        phone = request.session.get("phone")
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
