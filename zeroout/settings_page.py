"""What the Settings page does, apart from the routes: check a section, write the config file, reload.

It shares `zeroout setup`'s checks (the Token Factory model list, the Twilio account fetch, the caller ID
ready check, the validation request) instead of copying them. Every outside effect is a field of `Services`,
so tests run it with fakes. Secrets go into the config file and nowhere else: no context built here holds one.
"""
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from twilio.base.exceptions import TwilioException

from . import config, home, setup
from .paths import normalize_number

CHECKS_PER_HOUR = 60
READS_PER_HOUR = 600  # background loads and the verify poll; a page reload must never lock out a save
VERIFY_FOR = setup.POLL_FOR
POLL_SECONDS = setup.POLL_EVERY
# The only fields of the running Settings this page changes. The session secret, the cookie flag, the
# database path and the config path stay as the process started with them.
APPLIED = (
    "tf_api_key", "tf_base_url", "model_nano", "model_super", "twilio_sid", "twilio_token",
    "twilio_from", "twilio_verify_sid", "owner_number", "asr_model_dir", "daily_cap_usd", "community_repo",
)
LOCKED_MSG = "Set in your environment as {key}; change it there."
NOT_SAVED = "Nothing was saved."
TWILIO_KEYS = ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_VERIFY_SID", "OWNER_NUMBER")
NUMBER_CACHE_SECONDS = 60
SPEECH_LABELS = {
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": (
        "Parakeet v3, recommended", "About 470 MB, 25 languages including Spanish, about 1.1 GB of RAM."),
    "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8": (
        "Parakeet 110M, English only", "About 100 MB, English only and less accurate; Spanish menu options will be missed."),
}


def _twilio_client(sid: str, token: str) -> Any:
    from twilio.rest import Client

    return Client(sid, token)


def _verify_model(path: str) -> Any:
    from . import hear

    return hear._verify(path)


def _github_login() -> str | None:
    from . import github

    return github.github_login()


@dataclass
class Services:
    http: Callable[..., Any] = setup.open_no_redirect
    twilio: Callable[[str, str], Any] = _twilio_client
    clock: Callable[[], float] = time.monotonic
    verify_model: Callable[[str], Any] = _verify_model
    github_login: Callable[[], str | None] = _github_login


def config_target() -> Path:
    """The file the running app loaded: ZEROOUT_ENV_FILE when it names a file, else the per-user file."""
    named = os.environ.get("ZEROOUT_ENV_FILE", "").strip()
    if named and Path(named).is_file():
        return Path(named)
    return home.config_file()


def twilio_files() -> list[Path]:
    """Every config file that sets a Twilio key, since load_settings falls through from one file to the next."""
    seen, out = set(), []
    for f in config.env_files():
        key = os.path.realpath(f)
        if key not in seen and any((file_values(f).get(k) or "").strip() for k in TWILIO_KEYS):
            seen.add(key)
            out.append(f)
    return out


def file_values(path: Path) -> dict:
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}


def locked(key: str) -> bool:
    """A key set in the real environment wins over every file, so a file edit would change nothing."""
    return bool(os.environ.get(key, "").strip())


def tail(secret: str) -> str:
    """The last four characters, shown only when they are a small part of a long key."""
    return secret[-4:] if len(secret) >= 12 else ""


class SettingsPage:
    def __init__(self, settings: config.Settings, services: Services | None = None):
        self.settings = settings
        self.services = services or Services()
        self.lock = threading.RLock()
        self.job: dict | None = None  # the one verification that may be running
        self.checked: dict[str, tuple[float, bool]] = {}  # number -> (when, verified), for NUMBER_CACHE_SECONDS

    # ---------- shared pieces ----------

    def _remember(self, number: str, ready: bool) -> None:
        self.checked[number] = (self.services.clock(), ready)

    def _scrub(self, text: str, *extra: str) -> str:
        for secret in (self.settings.tf_api_key, self.settings.twilio_token, *extra):
            if secret and len(secret) >= 4:
                text = text.replace(secret, "(hidden)")
        return text

    def _field(self, name: str, label: str, key: str, value: str = "", *, errors: dict, secret: bool = False,
               current: str = "", hint: str = "", options: list[str] | None = None) -> dict:
        is_locked = locked(key)
        return {
            "name": name, "label": label, "key": key, "secret": secret, "locked": is_locked,
            "kind": "select" if options is not None else "text",
            "value": "" if secret else value, "options": options or [],
            "saved": bool(current), "tail": tail(current) if secret and not is_locked else "",
            "hint": hint, "error": errors.get(name, ""), "bad": name in errors,
        }

    def _resolve(self, errors: dict, form: dict, name: str, key: str, current: str, secret: bool = False) -> str:
        """The value to use: the environment's if the key is locked, else the form's, else what is saved."""
        raw = (form.get(name) or "").strip()
        if locked(key):
            if raw and (secret or raw != current):
                errors[name] = LOCKED_MSG.format(key=key)
            return current
        if "\n" in raw or "\r" in raw:
            errors[name] = f"{key} cannot contain a line break."
            return current
        return raw or current

    def _commit(self, updates: dict[str, str], paths: list[Path] | None = None) -> str | None:
        """Writes the file, reloads it and copies the managed fields into the running Settings.

        Returns a sentence when something went wrong. If the file was written but cannot be read back,
        the running settings are left as they were.
        """
        with self.lock:
            path = (paths or [config_target()])[0]
            try:
                rendered = [(p, setup.render({**file_values(p), **updates})) for p in (paths or [path])]
            except ValueError as e:
                return f"{NOT_SAVED} {e}"
            written = []
            for path, text in rendered:
                try:
                    setup.write_config(path, text)
                except OSError as e:
                    why = e.strerror or e
                    if not written:
                        return f"{NOT_SAVED} Could not write {path}: {why}."
                    # Some files are already changed, so reload what is on disk and say exactly what happened.
                    self._reload()
                    return (f"Changed {', '.join(map(str, written))}. Could not write {path}: {why}. "
                            f"{path} keeps its old values.")
                written.append(path)
            return self._reload(path)

    def _reload(self, path: Path | None = None) -> str | None:
        with self.lock:
            try:
                fresh = config.load_settings()
            except (ValueError, OSError) as e:
                return f"Saved to {path}, but the running app could not read it back: {str(e).rstrip('.')}. Changes apply after you restart zeroout run."
            for attr in APPLIED:
                setattr(self.settings, attr, getattr(fresh, attr))
        return None

    def _done(self, ctx: dict, *secrets: str) -> dict:
        for k in ("result", "error"):
            if ctx.get(k):
                ctx[k] = self._scrub(ctx[k], *secrets)
        for f in ctx.get("fields", {}).values():
            f["error"] = self._scrub(f["error"], *secrets)
        return ctx

    def _fail(self, ctx: dict, message: str, *secrets: str) -> dict:
        ctx.update(ok=False, result=message)
        return self._done(ctx, *secrets)

    def _saved(self, ctx: dict, message: str, failed: str | None, *secrets: str) -> dict:
        ctx.update(ok=failed is None, result=failed or message)
        return self._done(ctx, *secrets)

    # ---------- the page ----------

    def page(self) -> dict:
        return {
            "file": str(config_target()),
            "model": self.model_ctx(), "phone": self.phone_ctx(), "speech": self.speech_ctx(),
            "spending": self.spending_ctx(), "community": self.community_ctx(),
        }

    # ---------- language model ----------

    def model_ctx(self, form: dict | None = None, errors: dict | None = None, ids: list[str] | None = None,
                  result: str = "", ok: bool = True) -> dict:
        s, form, errors = self.settings, form or {}, errors or {}
        nano = (form.get("nano") or s.model_nano or setup.DEFAULT_NANO).strip()
        sup = (form.get("super") or s.model_super or setup.DEFAULT_SUPER).strip()
        listed = setup.nemotron_ids(ids) if ids is not None else None
        pick = lambda want: sorted({*(listed or []), want}) if listed is not None else [want]
        fields = {
            "api_key": self._field("api_key", "API key", "TF_API_KEY", errors=errors, secret=True,
                                   current=s.tf_api_key),
            "base_url": self._field("base_url", "Base URL", "TF_BASE_URL",
                                    (form.get("base_url") or s.tf_base_url or setup.DEFAULT_BASE).strip(), errors=errors,
                                    hint="Must start with https://, so the key is never sent in the clear."),
            "nano": self._field("nano", "Nano model", "MODEL_NANO", nano, errors=errors, options=pick(nano),
                                hint="" if listed is not None else "Check the key to choose from your Token Factory list."),
            "super": self._field("super", "Super model", "MODEL_SUPER", sup, errors=errors, options=pick(sup)),
        }
        return {"section": "model", "fields": fields, "result": result, "ok": ok, "on": bool(s.tf_api_key),
                "listed": listed is not None}

    def model(self, form: dict, save: bool) -> dict:
        s, errors = self.settings, {}
        key = self._resolve(errors, form, "api_key", "TF_API_KEY", s.tf_api_key, secret=True)
        base = self._resolve(errors, form, "base_url", "TF_BASE_URL", s.tf_base_url or setup.DEFAULT_BASE)
        nano = self._resolve(errors, form, "nano", "MODEL_NANO", s.model_nano or setup.DEFAULT_NANO)
        sup = self._resolve(errors, form, "super", "MODEL_SUPER", s.model_super or setup.DEFAULT_SUPER)
        submitted = (form.get("api_key") or "").strip()
        view = {**form, "base_url": base, "nano": nano, "super": sup}

        if not key and "api_key" not in errors:
            errors["api_key"] = "An API key is required. Zero-Out reads menus with models served there."
        if "base_url" not in errors and (problem := setup.base_problem(base)):
            errors["base_url"] = problem
        if errors:
            return self._fail(self.model_ctx(view, errors), f"{NOT_SAVED} Fix the fields marked below.", submitted)
        try:
            ids = setup.list_models(self.services.http, base, key)
        except setup.TfError as e:
            if e.auth:
                errors["api_key"] = str(e)
            return self._fail(self.model_ctx(view, errors), f"{NOT_SAVED} {e}", submitted, key)
        if not setup.nemotron_ids(ids):
            return self._fail(self.model_ctx(view, errors, ids),
                              f"{NOT_SAVED} No Nemotron model is listed for this key, so Zero-Out cannot run on it.", key)
        listed = f"Key accepted, {len(ids)} model{'' if len(ids) == 1 else 's'} listed"
        if not save:
            return self._done(self.model_ctx(view, errors, ids, f"{listed} · choose the Nano and Super models, then save", True), key)
        for name, label, want in (("nano", "Nano", nano), ("super", "Super", sup)):
            if want not in ids and not locked("MODEL_NANO" if name == "nano" else "MODEL_SUPER"):
                errors[name] = f"{label} model {want} is not in your Token Factory list. Choose one from the list."
        if errors:
            return self._fail(self.model_ctx(view, errors, ids), f"{NOT_SAVED} Fix the fields marked below.", key)
        updates = {"TF_API_KEY": key, "TF_BASE_URL": base, "MODEL_NANO": nano, "MODEL_SUPER": sup}
        failed = self._commit({k: v for k, v in updates.items() if not locked(k)})
        ctx = self.model_ctx({}, {}, ids)
        return self._saved(ctx, f"Saved. {listed} · the key goes only to Token Factory", failed, key, submitted)

    # ---------- phone calls ----------

    def phone_ctx(self, form: dict | None = None, errors: dict | None = None, result: str = "", ok: bool = True,
                  verifying: dict | None = None) -> dict:
        s, form, errors = self.settings, form or {}, errors or {}
        number = (form.get("number") or s.owner_number).strip()
        fields = {
            "sid": self._field("sid", "Account SID", "TWILIO_SID", (form.get("sid") or s.twilio_sid).strip(), errors=errors,
                               hint="AC followed by 32 hex characters, from the Twilio Console."),
            "token": self._field("token", "Auth token", "TWILIO_TOKEN", errors=errors, secret=True, current=s.twilio_token),
            "number": self._field("number", "Your own phone number", "OWNER_NUMBER", number, errors=errors,
                                  hint="Twilio calls this number first, then the company."),
        }
        return {"section": "phone", "fields": fields, "result": result, "ok": ok,
                "on": bool(s.twilio_sid and s.twilio_token), "number": normalize_or_empty(s.owner_number),
                "can_stop": any(getattr(s, a) for a in ("twilio_sid", "twilio_token", "twilio_from", "twilio_verify_sid", "owner_number")),
                "verifying": verifying}

    def phone(self, form: dict) -> dict:
        s, errors = self.settings, {}
        sid = self._resolve(errors, form, "sid", "TWILIO_SID", s.twilio_sid)
        token = self._resolve(errors, form, "token", "TWILIO_TOKEN", s.twilio_token, secret=True)
        raw = self._resolve(errors, form, "number", "OWNER_NUMBER", s.owner_number)
        submitted = (form.get("token") or "").strip()
        view = {**form, "sid": sid, "number": raw}
        if "sid" not in errors and (problem := setup.sid_problem(sid)):
            errors["sid"] = problem
        if "token" not in errors and not token:
            errors["token"] = "An auth token is required. Zero-Out uses it only to place calls through your Twilio account."
        e164 = ""
        if "number" not in errors:
            try:
                e164 = normalize_number(raw)
            except ValueError as e:
                errors["number"] = f"{e}. Got {raw}." if raw else "Enter your own phone number, like (415) 555-0100."
        if errors:
            return self._fail(self.phone_ctx(view, errors), f"{NOT_SAVED} Fix the fields marked below.", submitted, token)
        try:
            client, account = setup.fetch_account(self.services.twilio, sid, token)
        except (TwilioException, OSError) as e:
            auth, message = setup._twilio_error(e)
            if auth:
                errors["token"] = "Check the Account SID and the auth token."
            return self._fail(self.phone_ctx(view, errors), f"{NOT_SAVED} {message}", submitted, token)
        try:
            ready: bool | None = setup._is_ready(client, e164)
        except (TwilioException, OSError):
            ready = None
        if ready is not None:
            self._remember(e164, ready)
        updates = {"TWILIO_SID": sid, "TWILIO_TOKEN": token, "TWILIO_FROM": e164, "OWNER_NUMBER": e164}
        failed = self._commit({k: v for k, v in updates.items() if not locked(k)})
        kind = getattr(account, "type", "") or "unknown type"
        message = f"Saved. Twilio account {sid[:4]}...{sid[-4:]} checked ({kind}) · the token goes only to Twilio"
        if str(kind).lower() == "trial":
            message += f". {setup.TRIAL_WARNING}"
        if ready is False:
            message += f". {e164} is not verified with Twilio yet; choose Verify my number"
        return self._saved(self.phone_ctx(), message + ("" if message.endswith(".") else "."), failed, token, submitted)

    def stop(self) -> dict:
        """Clears every Twilio key. Mapping with your own phone on speaker needs none of them."""
        for key in TWILIO_KEYS:
            if locked(key):
                return self.phone_ctx(result=f"{NOT_SAVED} {LOCKED_MSG.format(key=key)}", ok=False)
        with self.lock:
            files = twilio_files() or [config_target()]
            failed = self._commit({k: "" for k in TWILIO_KEYS}, files)
            if failed is None:
                self.job, self.checked = None, {}
                back = [a for a in ("twilio_sid", "twilio_token", "twilio_from", "twilio_verify_sid", "owner_number")
                        if getattr(self.settings, a)]
                if back:
                    # The files were written, yet a key still loads: say where from, and do not claim success.
                    left = [str(f) for f in config.env_files() if any((file_values(f).get(k) or "").strip() for k in TWILIO_KEYS)]
                    failed = (f"Twilio is still set after clearing {', '.join(map(str, files))}, from {', '.join(left) or 'another source'}. "
                              "Clear it there, then try again.")
        names = ", ".join(map(str, files))
        return self._saved(self.phone_ctx(), f"Saved. Twilio is off in {names}. Mapping with your own phone on speaker still works "
                           "without Twilio", failed)

    def number(self) -> dict:
        """The saved number's state, asked of Twilio at most once a minute."""
        s = self.settings
        number = normalize_or_empty(s.owner_number)
        if not (s.twilio_sid and s.twilio_token and number):
            return {"state": "none"}
        hit = self.checked.get(number)
        if hit and self.services.clock() - hit[0] < NUMBER_CACHE_SECONDS:
            return {"state": "verified" if hit[1] else "unverified", "number": number}
        try:
            ready = setup._is_ready(self.services.twilio(s.twilio_sid, s.twilio_token), number)
        except (TwilioException, OSError) as e:
            return {"state": "error", "number": number, "reason": self._scrub(setup._twilio_error(e)[1])}
        self._remember(number, ready)
        return {"state": "verified" if ready else "unverified", "number": number}

    def _expired(self) -> bool:
        return self.services.clock() - self.job["started"] >= VERIFY_FOR

    def verify(self) -> dict:
        s = self.settings
        with self.lock:
            if self.job and not self._expired():
                return self.phone_ctx(result=f"Already verifying {self.job['number']}. Type the code on the keypad when Twilio calls.",
                                      ok=False, verifying=self._view_job())
            self.job = None
            number = normalize_or_empty(s.owner_number)
            if not (s.twilio_sid and s.twilio_token and number):
                return self.phone_ctx(result="Save your Account SID, auth token and phone number first.", ok=False)
            try:
                client = self.services.twilio(s.twilio_sid, s.twilio_token)
                if setup._is_ready(client, number):
                    self._remember(number, True)
                    return self.phone_ctx(result=f"{number} is already verified with Twilio.")
                code = setup.request_validation(client, number)
            except (TwilioException, OSError) as e:
                return self._done(self.phone_ctx(result=setup._twilio_error(e)[1], ok=False))
            self.job = {"number": number, "code": code, "client": client, "started": self.services.clock()}
            return self.phone_ctx(verifying=self._view_job())

    def _view_job(self) -> dict:
        return {"number": self.job["number"], "code": self.job["code"]}

    def verify_status(self) -> dict:
        with self.lock:
            job = self.job
            if job is None:
                return self.phone_ctx()
            number = job["number"]
            try:
                ready = setup._is_ready(job["client"], number)
            except (TwilioException, OSError) as e:
                self.job = None
                return self._done(self.phone_ctx(result=setup._twilio_error(e)[1], ok=False))
            if ready:
                self.job = None
                self._remember(number, True)
                return self.phone_ctx(result=f"{number} is verified with Twilio.")
            if self._expired():
                self.job = None
                self._remember(number, False)
                return self.phone_ctx(
                    result=f"Twilio did not hear the code within {VERIFY_FOR} seconds, so {number} is not verified. "
                           "Choose Verify my number to try again.", ok=False)
            return self.phone_ctx(verifying=self._view_job())

    # ---------- speech ----------

    def speech_choices(self) -> list[dict]:
        """One row per known model. A model counts as installed in models_dir or in the configured folder."""
        from . import hear

        root = Path(os.path.abspath(home.models_dir()))
        current = Path(os.path.abspath(self.settings.asr_model_dir)) if self.settings.asr_model_dir else None
        out = []
        for name, files in hear.MODELS.items():
            path = root / name
            if current is not None and current.name == name and all((current / f).is_file() for f in files):
                path = current  # the folder in use may live outside models_dir
            title, note = SPEECH_LABELS.get(name, (name, ""))
            out.append({"id": name, "name": title, "note": note, "path": str(path),
                        "installed": all((path / f).is_file() for f in files),
                        "current": current is not None and str(path) == str(current)})
        return out

    def speech_ctx(self, result: str = "", ok: bool = True) -> dict:
        return {"section": "speech", "choices": self.speech_choices(), "result": result, "ok": ok,
                "locked": locked("ASR_MODEL_DIR"), "on": os.path.isdir(self.settings.asr_model_dir or "")}

    def speech(self, form: dict) -> dict:
        choice = next((c for c in self.speech_choices() if c["id"] == (form.get("model") or "")), None)
        if choice is None:
            return self.speech_ctx(f"{NOT_SAVED} Choose one of the models listed.", False)
        if not choice["installed"]:
            return self.speech_ctx(f"{NOT_SAVED} {choice['name']} is not installed. Download it with zeroout setup.", False)
        if locked("ASR_MODEL_DIR"):
            return self.speech_ctx(f"{NOT_SAVED} {LOCKED_MSG.format(key='ASR_MODEL_DIR')}", False)
        try:
            self.services.verify_model(choice["path"])
        except Exception as e:
            return self.speech_ctx(f"{NOT_SAVED} {e}", False)
        failed = self._commit({"ASR_MODEL_DIR": choice["path"]})
        if failed is None:
            try:
                from . import hear

                hear.release_except(choice["path"])
            except Exception:
                pass
        ctx = self.speech_ctx()
        ctx.update(ok=failed is None, result=failed or f"Saved. {choice['name']} is used from the next clip · audio never leaves this computer")
        return ctx

    # ---------- one-field sections ----------

    def _single(self, key: str, raw: str, current: str, check: Callable[[str], str]) -> tuple[str, str]:
        """(value to save, error sentence) for a section with a single field."""
        raw = (raw or "").strip()
        if locked(key):
            return current, LOCKED_MSG.format(key=key) if raw and raw != current else ""
        if "\n" in raw or "\r" in raw:
            return raw, f"{key} cannot contain a line break."
        return raw, check(raw)

    def spending_ctx(self, value: str | None = None, errors: dict | None = None, result: str = "", ok: bool = True) -> dict:
        cap = self.settings.daily_cap_usd
        shown = value if value is not None else (f"{cap:g}" if cap else "")
        fields = {"cap": self._field("cap", "Daily spend cap in USD", "DAILY_CAP_USD", shown, errors=errors or {},
                                     hint="Zero-Out stops placing paid calls for the day once this is spent.")}
        return {"section": "spending", "fields": fields, "result": result, "ok": ok, "on": bool(cap)}

    def spending(self, form: dict) -> dict:
        raw, problem = self._single("DAILY_CAP_USD", form.get("cap", ""), f"{self.settings.daily_cap_usd:g}", setup.cap_problem)
        if problem:
            return self.spending_ctx(raw, {"cap": problem}, f"{NOT_SAVED} Fix the field marked below.", False)
        failed = None if locked("DAILY_CAP_USD") else self._commit({"DAILY_CAP_USD": raw})
        return self.spending_ctx(result=failed or f"Saved. At most ${self.settings.daily_cap_usd:g} a day is spent on calls", ok=failed is None)

    @staticmethod
    def _repo_problem(repo: str) -> str:
        if config.valid_repo(repo) and ".." not in repo:
            return ""
        return f"The community repo must look like owner/name, for example MAXIMUSSCORP/zero-out-trees. Got {repo or 'nothing'}."

    def community_ctx(self, value: str | None = None, errors: dict | None = None, result: str = "", ok: bool = True) -> dict:
        shown = value if value is not None else self.settings.community_repo
        fields = {"repo": self._field("repo", "Community repo", "COMMUNITY_REPO", shown, errors=errors or {},
                                      hint="owner/name on GitHub. What you share and what you update from lives here.")}
        return {"section": "community", "fields": fields, "result": result, "ok": ok, "on": True}

    def community(self, form: dict) -> dict:
        raw, problem = self._single("COMMUNITY_REPO", form.get("repo", ""), self.settings.community_repo, self._repo_problem)
        if problem:
            return self.community_ctx(raw, {"repo": problem}, f"{NOT_SAVED} Fix the field marked below.", False)
        failed = None if locked("COMMUNITY_REPO") else self._commit({"COMMUNITY_REPO": raw})
        return self.community_ctx(result=failed or f"Saved. Sharing and updating now use {self.settings.community_repo}", ok=failed is None)

    def account(self) -> dict:
        try:
            login = self.services.github_login()
        except Exception:
            login = None
        return {"login": login}


def normalize_or_empty(raw: str) -> str:
    try:
        return normalize_number(raw) if raw else ""
    except ValueError:
        return ""
