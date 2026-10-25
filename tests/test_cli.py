import argparse
import sys
import types

import pytest

from zeroout import cli, home
from zeroout.config import Settings


class FakeUvicorn:
    def __init__(self):
        self.calls = []

    def run(self, app, **kw):
        self.calls.append((app, kw))


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZEROOUT_HOME", str(tmp_path / "zhome"))
    return tmp_path


def configured(box):
    (box / "zhome").mkdir(exist_ok=True)
    home.config_file().write_text("SESSION_SECRET=" + "s" * 48 + "\n")


def args(port=8000, no_browser=True):
    return argparse.Namespace(port=port, no_browser=no_browser)


def patch_app(monkeypatch, fail=None):
    seen = {}

    def fake_create(settings, **kw):
        if fail:
            raise RuntimeError(fail)
        seen.update(settings=settings, **kw)
        return "APP"

    monkeypatch.setattr("zeroout.web.create_app", fake_create)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(
        "", "", "", "", "", "", "", "", 1.0, "x" * 48, config_path="/c/zeroout.env"))
    return seen


def test_not_set_up_exits_with_message(box, capsys):
    fake = FakeUvicorn()
    assert cli.run(args(), uvicorn=fake) == 1
    assert capsys.readouterr().out == "  Not set up yet. Run: zeroout setup\n"
    assert not fake.calls


def test_stray_cwd_env_does_not_count_as_set_up(box, capsys):
    (box / ".env").write_text("X=1\n")
    assert cli.main(["run", "--no-browser"]) == 1
    assert "Not set up yet" in capsys.readouterr().out


def test_env_file_variable_counts_as_set_up(box, monkeypatch):
    monkeypatch.setenv("ZEROOUT_ENV_FILE", ".env")
    patch_app(monkeypatch)
    fake = FakeUvicorn()
    assert cli.run(args(), uvicorn=fake) == 0 and fake.calls


def test_run_binds_localhost_without_proxy_headers_and_prints_token_url(box, monkeypatch, capsys):
    configured(box)
    seen = patch_app(monkeypatch)
    fake = FakeUvicorn()
    assert cli.run(args(port=9001), uvicorn=fake) == 0
    app, kw = fake.calls[0]
    assert app == "APP" and kw == {"host": "127.0.0.1", "port": 9001, "proxy_headers": False}
    assert seen["settings"].cookie_secure is False
    guard = seen["local"]
    assert guard.port == 9001 and len(guard.token) >= 32
    out = capsys.readouterr().out
    assert "  Settings  /c/zeroout.env\n" in out
    assert f"  Open      http://127.0.0.1:9001/start?t={guard.token}\n" in out


def test_token_differs_per_launch(box, monkeypatch):
    configured(box)
    seen = patch_app(monkeypatch)
    cli.run(args(), uvicorn=FakeUvicorn())
    first = seen["local"].token
    cli.run(args(), uvicorn=FakeUvicorn())
    assert seen["local"].token != first


def test_startup_error_is_one_sentence_and_no_open_line(box, monkeypatch, capsys):
    configured(box)
    patch_app(monkeypatch, fail="SESSION_SECRET must be at least 32 characters")
    fake = FakeUvicorn()
    assert cli.run(args(), uvicorn=fake) == 1
    assert capsys.readouterr().out == "  SESSION_SECRET must be at least 32 characters\n"
    assert not fake.calls


def test_data_folders_are_private(box, monkeypatch):
    configured(box)
    (box / "zhome").chmod(0o755)
    patch_app(monkeypatch)
    cli.run(args(), uvicorn=FakeUvicorn())
    assert (box / "zhome").stat().st_mode & 0o777 == 0o700


def test_browser_opens_token_url_unless_disabled(box, monkeypatch):
    configured(box)
    seen = patch_app(monkeypatch)
    monkeypatch.setattr(cli, "BROWSER_DELAY", 0)
    opened = []
    cli.run(args(no_browser=True), uvicorn=FakeUvicorn(), browser=opened.append)
    assert opened == []
    cli.run(args(no_browser=False), uvicorn=FakeUvicorn(), browser=opened.append)
    import time
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == [f"http://127.0.0.1:8000/start?t={seen['local'].token}"]


def test_no_host_flag():
    with pytest.raises(SystemExit):
        cli.main(["run", "--host", "0.0.0.0"])


def test_setup_and_crawl_dispatch(monkeypatch):
    got = {}
    for name in ("setup", "crawl"):
        mod = types.ModuleType(f"zeroout.{name}")
        mod.main = lambda rest, name=name: got.update({name: rest})
        monkeypatch.setitem(sys.modules, f"zeroout.{name}", mod)
        monkeypatch.setattr(__import__("zeroout"), name, mod, raising=False)
    assert cli.main(["setup", "--yes"]) == 0
    assert cli.main(["crawl", "--company-id", "3"]) == 0
    assert got == {"setup": ["--yes"], "crawl": ["--company-id", "3"]}
