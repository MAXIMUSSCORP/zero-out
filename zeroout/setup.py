"""`zeroout setup`: an interactive first-run wizard that writes the per-user config file.

Every outside effect (prompts, network, Twilio, model download, clock) is a field of
`Env`, so tests run the whole flow with fakes. Only `main()` builds the real one.
Nothing is written until every step has finished.
"""
import argparse
import difflib
import getpass
import json
import math
import os
import re
import secrets
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.client import InvalidURL
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from twilio.base.exceptions import TwilioException

from zeroout.paths import normalize_number

DEFAULT_BASE = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
DEFAULT_SUPER = "nvidia/nemotron-3-super-120b-a12b"
SPEECH_MODELS = (
    ("sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8", "Parakeet v3, about 470 MB download, 25 languages including Spanish, about 1.1 GB of RAM (recommended)"),
    ("sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8", "Parakeet 110M, about 100 MB, English only and less accurate; Spanish menu options will be missed"),
)
POLL_EVERY = 3
POLL_FOR = 120
TRIAL_WARNING = (
    "This Twilio account is a trial. Trial accounts can only call numbers you have verified, "
    "so Zero-Out cannot call companies until you upgrade it in the Twilio Console."
)
_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]*")


class Cancelled(Exception):
    pass


class TfError(ValueError):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


@dataclass
class Env:
    ask: Callable[[str], str]
    secret: Callable[[str], str]
    out: Callable[[str], None]
    http: Callable[..., Any]
    twilio: Callable[[str, str], Any]
    install: Callable[..., Path]
    verify: Callable[[str], Any]
    config_path: Path
    models_dir: Path
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    skip_model: bool = False
    new_secret: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(48))
    data_dir: Path | None = None
    next_cmd: str = "zeroout run"
    pending: str = ""  # set by the Twilio step when the saved number is not a verified caller ID


def mask(value: str) -> str:
    return "****" + value[-4:] if len(value) > 4 else "****" if value else ""


def say(env: Env, verb: str, text: str = "") -> None:
    env.out(f"  {verb:<9} {text}".rstrip())


def _prompt(env: Env, label: str, default: str = "", secret: bool = False) -> str:
    shown = mask(default) if secret else default
    text = f"  {label}" + (f" [{shown}]" if shown else "") + ": "
    while True:
        try:
            raw = (env.secret if secret else env.ask)(text)
        except EOFError:
            raise Cancelled from None
        raw = (raw or "").strip()
        if "\n" in raw or "\r" in raw:
            env.out("  A value cannot contain a line break, because the config file is one line per setting.")
            continue
        return raw or default


def _yes(env: Env, label: str, default: bool) -> bool:
    while True:
        raw = _prompt(env, f"{label} [{'Y/n' if default else 'y/N'}]").lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        env.out("  Answer y or n.")


def _pick(env: Env, count: int, default: int) -> int:
    while True:
        raw = _prompt(env, "Choose", str(default))
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        env.out(f"  Choose a number from 1 to {count}. Got {raw}.")


def _retry(env: Env, what: str) -> None:
    """Offer another go; quitting cancels the whole setup."""
    if not _yes(env, f"Try {what} again?", True):
        raise Cancelled


# ---------- Token Factory ----------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header to a redirect target, so a redirect would carry the key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            u = urlparse(newurl)
            where = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        except ValueError:
            where = "another address"
        raise TfError(f"Token Factory redirected to {where}; use that address as the base URL.")


def open_no_redirect(req: Any, timeout: float | None = None) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def list_models(http: Callable[..., Any], base: str, key: str) -> list[str]:
    """The live /models call; also used by the Settings page, which has no Env."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
        resp = http(req, timeout=20)
        try:
            body = resp.read()
        finally:
            getattr(resp, "close", lambda: None)()
    except TfError:
        raise
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TfError(f"Token Factory refused the key (HTTP {e.code}). Check it in the Token Factory console.", auth=True) from None
        raise TfError(f"Token Factory answered HTTP {e.code} at {base}.") from None
    except (urllib.error.URLError, OSError) as e:
        raise TfError(f"Could not reach Token Factory at {base}: {getattr(e, 'reason', e)}.") from None
    except (InvalidURL, ValueError):
        raise TfError(f"{base} is not an address Zero-Out can use. Check the base URL.") from None
    try:
        return [m["id"] for m in json.loads(body)["data"]]
    except (ValueError, KeyError, TypeError):
        raise TfError(f"{base} did not answer with a model list. Check the base URL.") from None


def _list_models(env: Env, base: str, key: str) -> list[str]:
    return list_models(env.http, base, key)


def nemotron_ids(ids: list[str]) -> list[str]:
    return [i for i in ids if "nemotron" in i.lower()]


def _choose_model(env: Env, label: str, want: str, ids: list[str]) -> str:
    if want in ids:
        say(env, "found", f"{label} model {want}")
        return want
    env.out(f"  {label} model {want} is not in your Token Factory list.")
    close = nemotron_ids(ids)
    close = difflib.get_close_matches(want, close, n=8, cutoff=0) if close else []
    if not close:
        env.out("  No Nemotron model is listed for this key, so Zero-Out cannot run on it.")
        raise Cancelled
    for n, i in enumerate(close, 1):
        env.out(f"    {n}  {i}")
    return close[_pick(env, len(close), 1) - 1]


def _base_ok(base: str) -> bool:
    try:
        u = urlparse(base)
    except ValueError:
        return False
    return bool(u.hostname) and (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")))


def base_problem(base: str) -> str:
    """A sentence when the base URL would send the key in the clear, else an empty string."""
    return "" if _base_ok(base) else f"The base URL must start with https://, so the key is never sent in the clear. Got {base}."


def step_token_factory(env: Env, cur: dict) -> dict:
    env.out("Token Factory")
    while True:
        key = _prompt(env, "API key", cur.get("TF_API_KEY", ""), secret=True)
        if not key:
            env.out("  An API key is required. Zero-Out reads menus with models served there.")
            continue
        while True:
            base = _prompt(env, "Base URL", cur.get("TF_BASE_URL") or DEFAULT_BASE)
            if not base_problem(base):
                break
            env.out(f"  {base_problem(base)}")
            cur = {**cur, "TF_BASE_URL": ""}
        try:
            ids = _list_models(env, base, key)
            break
        except TfError as e:
            env.out(f"  {e}")
            _retry(env, "the key")
            cur = {**cur, "TF_BASE_URL": base, "TF_API_KEY": "" if e.auth else key}
    say(env, "checked", f"key accepted, {len(ids)} models listed")
    return {
        "TF_API_KEY": key,
        "TF_BASE_URL": base,
        "MODEL_NANO": _choose_model(env, "Nano", cur.get("MODEL_NANO") or DEFAULT_NANO, ids),
        "MODEL_SUPER": _choose_model(env, "Super", cur.get("MODEL_SUPER") or DEFAULT_SUPER, ids),
    }


# ---------- speech model ----------


def _installed(env: Env, path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        env.verify(str(path))
    except Exception:
        return False
    return True


def step_speech(env: Env, cur: dict) -> dict:
    env.out("Speech model")
    env.out("  Menus are heard on this computer, so audio never leaves it.")
    for n, (name, note) in enumerate(SPEECH_MODELS, 1):
        env.out(f"    {n}  {note}")
    have = Path(cur.get("ASR_MODEL_DIR", "")).name
    default = next((n for n, (name, _) in enumerate(SPEECH_MODELS, 1) if name == have), 1)
    name = SPEECH_MODELS[_pick(env, len(SPEECH_MODELS), default) - 1][0]
    target = Path(os.path.abspath(env.models_dir / name))
    kept = cur.get("ASR_MODEL_DIR", "")
    if have == name and _installed(env, Path(kept)):
        say(env, "checked", f"{name} is already installed at {kept}")
        return {"ASR_MODEL_DIR": kept}
    if env.skip_model:
        say(env, "skipped", f"model download (--skip-model); copy the folder to {target} yourself")
    elif _installed(env, target):
        say(env, "checked", f"{name} is already installed")
    else:
        while True:
            try:
                target = Path(os.path.abspath(env.install(name, env.models_dir, progress=_progress(env))))
                break
            except (RuntimeError, OSError) as e:
                if isinstance(e, OSError):
                    e = f"The download stopped: {getattr(e, 'reason', None) or e}. Check your connection."
                env.out(f"  {e}")
                _retry(env, "the download")
        say(env, "installed", name)
    return {"ASR_MODEL_DIR": str(target)}


def _progress(env: Env) -> Callable[..., None]:
    last = [-1]

    def progress(done: float, total: float | None = None) -> None:
        if not total:
            return
        pct = min(100, int(done * 100 / total))
        if pct != last[0] and (pct % 10 == 0 or pct == 100):
            last[0] = pct
            say(env, "download", f"{pct}%")

    return progress


# ---------- Twilio ----------


def _twilio_error(e: Exception) -> tuple[bool, str]:
    """(is an auth failure, a sentence)."""
    status = getattr(e, "status", None)
    if status in (401, 403):
        return True, f"Twilio refused the Account SID or auth token (HTTP {status})."
    return False, f"Could not check with Twilio: {getattr(e, 'msg', None) or e}"


def sid_problem(sid: str) -> str:
    return "" if re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) else f"An Account SID is AC followed by 32 hex characters. Got {len(sid)} characters."


def fetch_account(factory: Callable[[str, str], Any], sid: str, token: str) -> tuple[Any, Any]:
    """(client, account). Raises TwilioException or OSError when Twilio refuses or cannot be reached."""
    client = factory(sid, token)
    return client, client.api.v2010.accounts(sid).fetch()


def request_validation(client: Any, e164: str) -> str:
    """Asks Twilio to call the number; returns the code to type on its keypad."""
    return client.validation_requests.create(phone_number=e164, friendly_name="Zero-Out").validation_code


def _is_ready(client: Any, e164: str) -> bool:
    return bool(client.outgoing_caller_ids.list(phone_number=e164) or client.incoming_phone_numbers.list(phone_number=e164))


def _verify_number(env: Env, client: Any, e164: str) -> bool:
    try:
        code = request_validation(client, e164)
    except TwilioException as e:
        env.out(f"  {_twilio_error(e)[1]}")
        return False
    env.out("")
    env.out(f"  Twilio is calling {e164} now. Answer and type this code on the keypad: {code}")
    env.out("")
    start = env.clock()
    try:
        while True:
            if client.outgoing_caller_ids.list(phone_number=e164):
                return True
            if env.clock() - start >= POLL_FOR:
                return False
            env.sleep(POLL_EVERY)
    except KeyboardInterrupt:
        env.out("  Stopped waiting. Your answers are kept.")
        return False


def step_twilio(env: Env, cur: dict) -> dict:
    off = {"TWILIO_SID": "", "TWILIO_TOKEN": "", "TWILIO_FROM": "", "OWNER_NUMBER": "", "TWILIO_VERIFY_SID": ""}
    env.out("Twilio (optional)")
    env.out("  Without Twilio you map menus with your own phone on speaker: open a company in zeroout run and choose Map this menu with my phone.")
    env.out("  With Twilio, Zero-Out dials, crawls and Connect me runs automatically, billed by Twilio per minute.")
    if not _yes(env, "Set up Twilio?", bool(cur.get("TWILIO_SID"))):
        return off
    while True:
        sid = _prompt(env, "Account SID", cur.get("TWILIO_SID", ""))
        if sid_problem(sid):
            env.out(f"  {sid_problem(sid)}")
            cur = {**cur, "TWILIO_SID": ""}
            continue
        token = _prompt(env, "Auth token", cur.get("TWILIO_TOKEN", ""), secret=True)
        try:
            client, account = fetch_account(env.twilio, sid, token)
            break
        except (TwilioException, OSError) as e:
            auth, msg = _twilio_error(e)
            env.out(f"  {msg}")
            saved = {k: cur.get(k, "") for k in off}
            if not auth and saved["TWILIO_SID"] == sid and saved["TWILIO_TOKEN"] == token and saved["OWNER_NUMBER"]:
                if _yes(env, "Keep the saved Twilio settings without checking them?", True):
                    return saved
            if not _yes(env, "Try the credentials again?", True):
                env.out("  Twilio left unconfigured; run setup again when you have them.")
                return off
            cur = {**cur, "TWILIO_TOKEN": ""} if auth else cur
    say(env, "checked", f"Twilio account {sid[:4]}...{sid[-4:]} ({getattr(account, 'type', '') or 'unknown type'})")
    if str(getattr(account, "type", "")).lower() == "trial":
        env.out(f"  {TRIAL_WARNING}")
    while True:
        try:
            e164 = normalize_number(_prompt(env, "Your own phone number", cur.get("OWNER_NUMBER", "")))
            break
        except ValueError as e:
            env.out(f"  {e}")
    try:
        ready = _is_ready(client, e164)
        if ready:
            say(env, "ready", f"{e164} is verified with Twilio")
        elif _yes(env, f"{e164} is not verified with Twilio yet. Verify it now?", True):
            if _verify_number(env, client, e164):
                say(env, "verified", e164)
            else:
                env.pending = e164
                env.out("  Not verified yet. In the Twilio Console open Phone Numbers > Verified Caller IDs,")
                env.out("  verify the number there, then run zeroout setup again.")
        else:
            env.pending = e164
            env.out("  Verify it in the Twilio Console under Phone Numbers > Verified Caller IDs, then run setup again.")
    except (TwilioException, OSError) as e:
        env.pending = e164
        env.out(f"  {_twilio_error(e)[1]}")
    return {**off, "TWILIO_SID": sid, "TWILIO_TOKEN": token, "TWILIO_FROM": e164, "OWNER_NUMBER": e164}


# ---------- limits, secret, file ----------


def cap_problem(raw: str) -> str:
    try:
        cap = float(raw)
        if math.isfinite(cap) and cap > 0:
            return ""
    except ValueError:
        pass
    return f"The daily cap must be a number above 0, like 5. Got {raw}."


def step_limits(env: Env, cur: dict) -> dict:
    env.out("Limits")
    while True:
        raw = _prompt(env, "Daily spend cap in USD", cur.get("DAILY_CAP_USD") or "5")
        if not cap_problem(raw):
            break
        env.out(f"  {cap_problem(raw)}")
    secret = cur.get("SESSION_SECRET", "")
    return {
        "DAILY_CAP_USD": raw,
        "RATE_PER_MIN": cur.get("RATE_PER_MIN") or "0.014",
        "SESSION_SECRET": secret if len(secret) >= 32 else env.new_secret(),
        "COOKIE_SECURE": "0",
    }


LAYOUT = (
    ("Nebius Token Factory", ("TF_API_KEY", "TF_BASE_URL", "MODEL_NANO", "MODEL_SUPER")),
    ("Local speech model", ("ASR_MODEL_DIR",)),
    ("Twilio, empty when unused", ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_VERIFY_SID", "OWNER_NUMBER")),
    ("Limits", ("DAILY_CAP_USD", "RATE_PER_MIN")),
    ("Session cookie; zeroout run is http on localhost", ("SESSION_SECRET", "COOKIE_SECURE")),
)


def quote(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot contain a line break.")
    if _SAFE.fullmatch(value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render(values: dict) -> str:
    known = {k for _, keys in LAYOUT for k in keys}
    blocks = [f"# {title}\n" + "".join(f"{k}={quote(k, values.get(k, ''))}\n" for k in keys) for title, keys in LAYOUT]
    extra = {k: v for k, v in values.items() if k not in known and v is not None}
    if extra:
        blocks.append("# Kept from the previous file\n" + "".join(f"{k}={quote(k, v)}\n" for k, v in extra.items()))
    return "# Written by zeroout setup. Run it again to change anything.\n\n" + "\n".join(blocks)


def write_config(path: Path, text: str) -> None:
    """Temp file beside the target, created 0600 before any secret goes in, then renamed over it."""
    path = Path(os.path.realpath(path))  # write through a symlink, so the link stays a link
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")  # mkstemp is 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- flow ----------


def run(env: Env) -> int:
    try:
        cur = {k: v for k, v in dotenv_values(env.config_path, interpolate=False).items() if v is not None} if env.config_path.exists() else {}
        env.out("Zero-Out setup. Press Enter to keep the value in brackets; Ctrl-C quits without saving.")
        values = dict(cur)
        for step in (step_token_factory, step_speech, step_twilio, step_limits):
            env.out("")
            values.update(step(env, values))
        text = render(values)
        write_config(env.config_path, text)
        if os.name == "posix":
            for d in (env.config_path.parent, env.data_dir):
                if d is not None and d.is_dir():
                    os.chmod(d, 0o700)
    except (KeyboardInterrupt, Cancelled):
        env.out("")
        env.out("Setup cancelled, nothing was saved.")
        return 1
    except (ValueError, OSError) as e:
        env.out(f"Setup could not save the config, so nothing was saved: {e}")
        return 1
    env.out("")
    say(env, "saved", str(env.config_path))
    if env.pending:
        say(env, "pending", f"{env.pending} is not a verified caller ID yet; calls will fail until you finish verifying it")
    say(env, "next", env.next_cmd)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="zeroout setup",
        description="Interactive setup: checks your keys, installs the speech model and writes the config file.",
        epilog="When stdin is not a terminal, the Token Factory key is read from the environment variable "
        "ZEROOUT_TF_KEY instead of a hidden prompt.",
    )
    ap.add_argument("--home", metavar="PATH", help="use PATH for the config, data and models of this run (like ZEROOUT_HOME)")
    ap.add_argument("--skip-model", action="store_true", help="do not download the speech model (you copy it in by hand)")
    args = ap.parse_args(argv)
    if args.home:
        os.environ["ZEROOUT_HOME"] = os.path.abspath(args.home)
    from zeroout import hear, home

    env_key = [None if sys.stdin.isatty() else os.environ.get("ZEROOUT_TF_KEY")]

    def secret(prompt: str) -> str:
        if env_key[0]:
            key, env_key[0] = env_key[0], None
            return key
        return getpass.getpass(prompt)

    def install(name: str, root: Path, **kw: Any) -> Path:
        return hear.install(name, root, **kw)

    def twilio(sid: str, token: str) -> Any:
        from twilio.rest import Client

        return Client(sid, token)

    return run(Env(
        ask=input,
        secret=secret,
        out=print,
        http=open_no_redirect,
        twilio=twilio,
        install=install,
        verify=hear._verify,
        config_path=home.config_file(),
        models_dir=home.models_dir(),
        skip_model=args.skip_model,
        data_dir=home.data_dir(),
        next_cmd=f"ZEROOUT_HOME={shlex.quote(os.environ['ZEROOUT_HOME'])} zeroout run" if args.home else "zeroout run",
    ))


if __name__ == "__main__":
    sys.exit(main())
