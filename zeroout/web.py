import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .ask import MAX_QUESTION, ask as ask_question
from .budget import can_spend
from .config import Settings, load_settings
from .crawl import crawl
from .paths import is_toll_free, normalize_number
from .tree import tree_json

TEMPLATES = Path(__file__).parent / "templates"
BUDGET_MSG = "Daily call budget reached, try again tomorrow"
TOLL_FREE_MSG = "Only US toll-free numbers (800, 888, 877, 866, 855, 844, 833)"
PATH_RE = re.compile(r"[0-9*#](\.[0-9*#])*")
HOUR = 3600


class RateLimiter:
    def __init__(self, limit: int, window: float = HOUR, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
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


def _annotate(conn, cid: int, node: dict) -> None:
    latest = {
        r["path"]: r["id"]
        for r in conn.execute(
            "SELECT path, MAX(id) AS id FROM observations "
            "WHERE company_id = ? AND audio_url IS NOT NULL GROUP BY path", (cid,))
    }

    def walk(n: dict) -> None:
        n["obs_id"] = latest.get(n["path"])
        for c in n["children"]:
            walk(c)

    walk(node)


def create_app(
    settings: Settings, conn=None, probe=None, listen=None, same=None, chat=None,
    connect=None, verify=None,
) -> FastAPI:
    conn = conn if conn is not None else db.connect(settings.db_path)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    app.state.running = set()
    lock = threading.Lock()
    ask_limit, connect_limit, verify_limit = RateLimiter(20), RateLimiter(3), RateLimiter(5)
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
        return lambda w: _listen(settings, w)

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
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown company")
        return row

    def ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def limited(limiter: RateLimiter, request: Request) -> None:
        if not limiter.allow(ip(request)):
            raise HTTPException(429, "Too many requests, try again later")

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("budget_ok", can_spend(conn, settings))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, msg: str, status: int) -> HTMLResponse:
        return render(request, "index.html", status, error=msg, companies=_companies())

    def _companies():
        return conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM nodes n WHERE n.company_id = c.id "
            "AND n.status != 'unexplored') AS mapped FROM companies c ORDER BY c.name"
        ).fetchall()

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return HTMLResponse(f"<p>{_esc(str(exc.detail))}</p>", status_code=exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html", companies=_companies(), error=None)

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def company_page(request: Request, cid: int):
        c = company(cid)
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
        company(cid)
        limited(ask_limit, request)
        ans = ask_question(conn, settings, cid, question[:MAX_QUESTION], chat=get_chat())
        return render(request, "_answer.html", answer=ans, keys=" → ".join(ans.path.split(".")) if ans.path else "")

    @app.post("/companies")
    def add_company(request: Request, name: str = Form(""), number: str = Form("")):
        name = name.strip()
        if not name or len(name) > 100:
            return error_page(request, "Enter a company name (up to 100 characters)", 400)
        try:
            e164 = normalize_number(number)
        except ValueError as e:
            return error_page(request, str(e), 400)
        if not is_toll_free(e164):
            return error_page(request, TOLL_FREE_MSG, 400)
        row = conn.execute("SELECT id FROM companies WHERE number = ?", (e164,)).fetchone()
        cid = row["id"] if row else db.add_company(conn, name, e164)
        return RedirectResponse(f"/c/{cid}", status_code=303)

    @app.post("/c/{cid}/crawl")
    def crawl_route(cid: int, tasks: BackgroundTasks):
        company(cid)
        with lock:
            if cid in app.state.running:
                raise HTTPException(409, "Already mapping this company")
            if not can_spend(conn, settings):
                raise HTTPException(503, BUDGET_MSG)
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
        try:
            e164 = normalize_number(phone)
        except ValueError as e:
            raise HTTPException(400, str(e))
        limited(verify_limit, request)
        try:
            get_verify().start(e164)
        except Exception:
            raise HTTPException(502, "Could not send the code, try again later")
        return HTMLResponse("<p>Code sent. Enter it below.</p>")

    @app.post("/verify/check")
    def verify_check(request: Request, phone: str = Form(""), code: str = Form("")):
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
        return tree_json(conn, cid) or {}

    @app.get("/audio/{obs_id}")
    def audio(obs_id: int):
        row = conn.execute("SELECT audio_url FROM observations WHERE id = ?", (obs_id,)).fetchone()
        if row is None or not row["audio_url"]:
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
