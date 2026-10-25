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


# ---------- contribute and update ----------

PUBLIC = ("This opens a public pull request on MAXIMUSSCORP/zero-out-trees as @zaid. The file is public as soon "
          "as you share it, before anyone reviews it, and GitHub keeps it even if the pull request is closed.")

import dataclasses
from types import SimpleNamespace

from zeroout import db, github
from zeroout.github import Submitted


class FakeCommunity:
    def __init__(self):
        self.contribution = SimpleNamespace(
            relpath="trees/8005550000/zaid-2026.json",
            doc={"name": "Acme", "number": "8005550000", "observations": [{"source": "phone"}, {"source": "crawl"}, {"source": "phone"}]},
            body=b"{}\n", observation_ids=[1, 2, 3])
        self.marked, self.imported = [], []
        self.report = SimpleNamespace(files=12, invalid=["trees/8005550000/bad.json"],
                                      observations_added=30, already_had=5, offered=3, truncated=False,
                                      capped=False, orphans=0, future=0, results={})

    def build_contribution(self, conn, company_id, *, now=None):
        return self.contribution

    def mark_contributed(self, conn, ids, when):
        self.marked.append(list(ids))

    def import_files(self, conn, files, *, same_meaning, max_model_checks=200, truncated=False):
        self.imported.append(list(files))
        self.report.truncated = truncated
        return self.report


@pytest.fixture
def share(box, monkeypatch):
    comm = FakeCommunity()
    monkeypatch.setitem(sys.modules, "zeroout.community", comm)
    import zeroout
    monkeypatch.setattr(zeroout, "community", comm, raising=False)
    st = Settings("", "", "", "", "", "", "", "", 1.0, "x" * 48, db_path=str(box / "d.db"))
    monkeypatch.setattr(cli, "load_settings", lambda: st)
    c = db.connect(st.db_path)
    cid = db.add_company(c, "Acme", "+18005550000")
    c.close()
    monkeypatch.setattr("zeroout.tree.nano_same_meaning", lambda s: (lambda a, b: True))
    monkeypatch.setattr(github, "github_login", lambda **kw: "zaid")
    sent = []
    monkeypatch.setattr(github, "submit", lambda con, repo, *, outbox_dir, **kw: sent.append((repo, outbox_dir)) or Submitted(
        "https://github.com/MAXIMUSSCORP/zero-out-trees/pull/3", None, "Opened"))
    share_truncated = [False]
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: github.Download([("trees/8005550000/a.json", b"{}")], share_truncated[0]))
    return SimpleNamespace(comm=comm, cid=cid, sent=sent, st=st, truncated=share_truncated)


def test_contribute_asks_then_submits_and_marks(share, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["contribute", "--company-id", str(share.cid)]) == 0
    out = capsys.readouterr().out
    assert "  Company   Acme (+18005550000)" in out and "3 observations" in out and "phone 2, crawl 1" not in out
    assert PUBLIC in out
    assert "crawl 1, phone 2" in out and "trees/8005550000/zaid-2026.json" in out
    assert "  Shared    https://github.com/MAXIMUSSCORP/zero-out-trees/pull/3" in out
    assert share.comm.marked == [[1, 2, 3]]
    assert share.sent[0][0] == "MAXIMUSSCORP/zero-out-trees" and share.sent[0][1].name == "outbox"


def test_contribute_declined_does_nothing(share, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli.main(["contribute", "--company-id", str(share.cid)]) == 0
    assert "  Not shared." in capsys.readouterr().out
    assert share.sent == [] and share.comm.marked == []


def test_contribute_yes_skips_the_question(share, monkeypatch, capsys):
    def no_input(prompt=""):
        raise AssertionError("must not ask")

    monkeypatch.setattr("builtins.input", no_input)
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 0
    assert share.comm.marked == [[1, 2, 3]]


def test_contribute_outbox_is_marked_and_named(share, monkeypatch, capsys):
    monkeypatch.setattr(github, "submit", lambda *a, outbox_dir, **k: Submitted(None, outbox_dir / "f.json", "GitHub CLI is not set up, so saved."))
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "  Saved     " in out and "GitHub CLI is not set up, so saved." in out
    assert share.comm.marked == [[1, 2, 3]]


def test_contribute_nothing_new_and_unknown_company(share, capsys):
    share.comm.contribution = None
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 0
    assert "  Nothing new to share for Acme." in capsys.readouterr().out
    assert cli.main(["contribute", "--company-id", "99", "--yes"]) == 1
    assert "  No company with id 99." in capsys.readouterr().out


def test_contribute_submit_error_exits_1_without_marking(share, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(github, "submit", boom)
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 1
    out = capsys.readouterr().out
    assert "  Something went wrong sharing that file." in out and "disk" not in out
    assert share.comm.marked == []


def test_contribute_needs_company_id(share):
    with pytest.raises(SystemExit):
        cli.main(["contribute"])


def test_update_prints_the_report(share, capsys):
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "  Checked   12 files" in out and "  Added     30 observations" in out
    assert "  Offered   3 numbers offered by the community" in out and "more files than one update" not in out
    assert "  Had       5 already" in out and "  Skipped   1 invalid file" in out
    assert "    trees/8005550000/bad.json" in out
    assert share.comm.imported == [[("trees/8005550000/a.json", b"{}")]]


def test_update_error_exits_1_with_a_sentence(share, monkeypatch, capsys):
    def missing(repo, **kw):
        raise github.CommunityError(f"The community repo {repo} was not found.")

    monkeypatch.setattr(github, "download_trees", missing)
    assert cli.main(["update"]) == 1
    assert capsys.readouterr().out == "  The community repo MAXIMUSSCORP/zero-out-trees was not found.\n"
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: (_ for _ in ()).throw(KeyError("secret")))
    assert cli.main(["update"]) == 1
    out = capsys.readouterr().out
    assert "  Something went wrong updating from the community." in out and "secret" not in out


def test_bad_community_repo_exits_1_with_a_sentence(box, monkeypatch, capsys):
    configured(box)
    monkeypatch.setenv("COMMUNITY_REPO", "not a repo")
    for argv in (["update"], ["contribute", "--company-id", "1"], ["run", "--no-browser"]):
        assert cli.main(argv) == 1
        assert "COMMUNITY_REPO" in capsys.readouterr().out


def test_yes_still_prints_that_the_share_is_public(share, capsys):
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 0
    out = capsys.readouterr().out
    assert PUBLIC in out and "  Account   @zaid" in out


def test_without_a_github_account_the_preview_says_it_goes_to_the_outbox(share, monkeypatch, capsys):
    monkeypatch.setattr(github, "github_login", lambda **kw: None)
    monkeypatch.setattr(github, "submit", lambda *a, outbox_dir, **k: Submitted(None, outbox_dir / "f.json", "saved"))
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "public pull request" not in out and "outbox folder" in out


def test_partial_result_is_reported_marked_and_exits_1(share, monkeypatch, capsys):
    msg = "The file is on your fork's branch contrib/8005550000-0123456789abcdef but the pull request was not opened: rate limited."
    monkeypatch.setattr(github, "submit", lambda *a, **k: Submitted(None, None, msg, partial=True))
    assert cli.main(["contribute", "--company-id", str(share.cid), "--yes"]) == 1
    assert msg in capsys.readouterr().out
    assert share.comm.marked == [[1, 2, 3]]


def test_yes_flag_help_says_it_skips_the_question(capsys):
    with pytest.raises(SystemExit):
        cli.main(["contribute", "--help"])
    out = capsys.readouterr().out
    assert "--yes" in out and "skip the question" in out and "scripts" in out


def test_update_says_when_truncated(share, capsys):
    share.truncated[0] = True
    assert cli.main(["update"]) == 0
    assert "  The community repo has more files than one update reads; some were not checked." in capsys.readouterr().out


def test_update_says_capped_and_orphans(share, capsys):
    share.comm.report.capped, share.comm.report.orphans = True, 4
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "  Some companies have more community data than one update brings in; run Update again later." in out
    assert "  Skipped 4 observations for menu options your tree does not have." in out


def test_contribute_prints_the_exact_file_before_asking(share, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(capsys.readouterr().out) or "n")
    assert cli.main(["contribute", "--company-id", str(share.cid)]) == 0
    before = asked[0]
    assert "{}" in before and "Check it for names before you share." in before
    assert before.index("{}") < before.index("Check it for names") < len(before)


def test_update_says_how_many_future_dated_observations_were_skipped(share, capsys):
    assert cli.main(["update"]) == 0
    assert "dated in the future" not in capsys.readouterr().out
    share.comm.report.future = 2
    assert cli.main(["update"]) == 0
    assert "  Skipped 2 observations dated in the future." in capsys.readouterr().out
