import os
import re
from pathlib import Path
from dataclasses import dataclass

from dotenv import dotenv_values

from . import home


@dataclass
class Settings:
    tf_api_key: str
    tf_base_url: str
    model_nano: str
    model_super: str
    twilio_sid: str
    twilio_token: str
    twilio_from: str
    twilio_verify_sid: str
    daily_cap_usd: float
    session_secret: str
    rate_per_min: float = 0.014
    db_path: str = "data/zeroout.db"
    audio_dir: str = "data/audio"
    # The user's own phone number (E.164), verified with Twilio as a caller ID.
    # When set, this person is the only caller and no SMS verification runs.
    owner_number: str = ""
    # The config file that was loaded, so `zeroout run` can say where settings came from.
    config_path: str = ""
    # Seconds added after a parent prompt before pressing the next key.
    key_margin: float = 1.5
    cookie_secure: bool = True
    asr_model_dir: str = "models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    community_repo: str = "MAXIMUSSCORP/zero-out-trees"


REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


def valid_repo(repo: str) -> bool:
    """owner/name, with no name made only of dots (GitHub reads those as paths, not repos)."""
    return bool(REPO_RE.fullmatch(repo)) and not repo.split("/")[1].strip(".") == ""


def env_files() -> list[Path]:
    """Files that exist, in priority order. A dev file counts only when the real environment names it."""
    named = os.environ.get("ZEROOUT_ENV_FILE", "").strip()
    candidates = ([Path(named)] if named else []) + [home.config_file()]
    return [p for p in candidates if p.is_file()]


def load_settings() -> Settings:
    """Real environment first, then ZEROOUT_ENV_FILE, then the per-user file.

    A key comes from the first source where it is set and not blank. Nothing is written
    to os.environ and nothing is read from the working directory.
    """
    files = env_files()
    sources = [os.environ] + [dotenv_values(p, interpolate=False) for p in files]

    def env(key: str, default: str = "") -> str:
        for src in sources:
            value = src.get(key)
            if value is not None and value.strip():
                return value
        return default

    repo = env("COMMUNITY_REPO", "MAXIMUSSCORP/zero-out-trees").strip()
    if not valid_repo(repo) or ".." in repo:
        raise ValueError(f"COMMUNITY_REPO must look like owner/name, for example MAXIMUSSCORP/zero-out-trees. Got {repo!r}.")
    return Settings(
        tf_api_key=env("TF_API_KEY", ""),
        tf_base_url=env("TF_BASE_URL", ""),
        model_nano=env("MODEL_NANO", ""),
        model_super=env("MODEL_SUPER", ""),
        twilio_sid=env("TWILIO_SID", ""),
        twilio_token=env("TWILIO_TOKEN", ""),
        twilio_from=env("TWILIO_FROM", ""),
        twilio_verify_sid=env("TWILIO_VERIFY_SID", ""),
        daily_cap_usd=float(env("DAILY_CAP_USD", "0")),
        session_secret=env("SESSION_SECRET", ""),
        rate_per_min=float(env("RATE_PER_MIN", "0.014")),
        db_path=env("DB_PATH", str(home.data_dir() / "zeroout.db")),
        audio_dir=env("AUDIO_DIR", str(home.data_dir() / "audio")),
        key_margin=float(env("KEY_MARGIN", "1.5")),
        cookie_secure=env("COOKIE_SECURE", "1").strip().lower() not in ("0", "false"),
        asr_model_dir=env(
            "ASR_MODEL_DIR", str(home.models_dir() / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")),
        owner_number=env("OWNER_NUMBER", "").strip(),
        community_repo=repo,
        config_path=", ".join(map(str, files)) or "environment",
    )
