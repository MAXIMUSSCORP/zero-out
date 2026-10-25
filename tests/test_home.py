from pathlib import Path

import pytest

from zeroout import home


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("ZEROOUT_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/h")))


def test_linux_defaults():
    assert home.config_dir("linux") == Path("/h/.config/zeroout")
    assert home.data_dir("linux") == Path("/h/.local/share/zeroout")


def test_linux_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x/c")
    monkeypatch.setenv("XDG_DATA_HOME", "/x/d")
    assert home.config_dir("linux") == Path("/x/c/zeroout")
    assert home.data_dir("linux") == Path("/x/d/zeroout")


def test_macos_config_and_data_match():
    want = Path("/h/Library/Application Support/zeroout")
    assert home.config_dir("darwin") == want and home.data_dir("darwin") == want


def test_windows(monkeypatch):
    monkeypatch.setenv("APPDATA", "/w/roam")
    monkeypatch.setenv("LOCALAPPDATA", "/w/local")
    assert home.config_dir("win32") == Path("/w/roam/zeroout")
    assert home.data_dir("win32") == Path("/w/local/zeroout")


def test_windows_without_env_falls_back_to_profile():
    assert home.config_dir("win32") == Path("/h/AppData/Roaming/zeroout")
    assert home.data_dir("win32") == Path("/h/AppData/Local/zeroout")


def test_override_wins_everywhere(monkeypatch):
    monkeypatch.setenv("ZEROOUT_HOME", "/p")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x")
    for plat in ("linux", "darwin", "win32"):
        assert home.config_dir(plat) == Path("/p") and home.data_dir(plat) == Path("/p")
    assert home.config_file() == Path("/p/zeroout.env")
    assert home.models_dir() == Path("/p/models")


def test_blank_override_is_ignored(monkeypatch):
    monkeypatch.setenv("ZEROOUT_HOME", "  ")
    assert home.config_dir("linux") == Path("/h/.config/zeroout")


def test_nothing_created(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEROOUT_HOME", str(tmp_path / "new"))
    home.config_file(), home.models_dir()
    assert not (tmp_path / "new").exists()
