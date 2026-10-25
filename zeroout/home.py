"""Per-user folders for settings, data and models. Only computes paths; creates nothing."""
import os
import sys
from pathlib import Path


def _override() -> Path | None:
    value = os.environ.get("ZEROOUT_HOME", "").strip()
    return Path(value).expanduser() if value else None


def config_dir(platform: str | None = None) -> Path:
    if (home := _override()) is not None:
        return home
    platform = platform or sys.platform
    if platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "zeroout"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "zeroout"
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "zeroout" if base else Path.home() / ".config" / "zeroout"


def data_dir(platform: str | None = None) -> Path:
    if (home := _override()) is not None:
        return home
    platform = platform or sys.platform
    if platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "zeroout"
    if platform == "darwin":
        return config_dir(platform)
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) / "zeroout" if base else Path.home() / ".local" / "share" / "zeroout"


def config_file() -> Path:
    return config_dir() / "zeroout.env"


def models_dir() -> Path:
    return data_dir() / "models"
