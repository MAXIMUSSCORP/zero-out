import os
from dataclasses import dataclass

from dotenv import load_dotenv


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
    # Seconds added after a parent prompt before pressing the next key.
    key_margin: float = 1.5
    cookie_secure: bool = True
    asr_model_dir: str = "models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
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
        db_path=env("DB_PATH", "data/zeroout.db"),
        audio_dir=env("AUDIO_DIR", "data/audio"),
        key_margin=float(env("KEY_MARGIN", "1.5")),
        cookie_secure=env("COOKIE_SECURE", "1").strip().lower() not in ("0", "false"),
        asr_model_dir=env("ASR_MODEL_DIR", "models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"),
    )
