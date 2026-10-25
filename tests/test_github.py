import base64
import io
import json
import subprocess
import tarfile
import urllib.error
from types import SimpleNamespace

import pytest

from zeroout import github
from zeroout.github import download_trees, submit

REPO = "MAXIMUSSCORP/zero-out-trees"
FILE_ID = "0123456789abcdef"
REL = f"trees/8005550000/2026-10-25-{FILE_ID}.json"
BRANCH = f"contrib/8005550000-{FILE_ID}"
SHA = "a" * 40
PR = "https://github.com/MAXIMUSSCORP/zero-out-trees/pull/7"


def contribution(rel=REL, body=b'{"name": "Acme"}\n'):
    return SimpleNamespace(relpath=rel, file_id=FILE_ID, doc={"name": "Acme", "observations": [{"source": "phone"}, {"source": "crawl"}]},
                           body=body, observation_ids=[1, 2, 3])


def done(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeGh:
    """Answers gh commands by their leading words; records every argv and its keyword arguments."""

    def __init__(self, answers=None, which=True):
        self.calls, self.kw, self.which = [], [], which
        self.answers = {
            ("gh", "auth", "status"): done(),
            ("gh", "api", "user"): done("zaid\n"),
            ("gh", "api", "repos/%s" % REPO): done("false\n"),
            ("gh", "repo", "fork"): done(),
            ("gh", "api", "repos/%s/forks" % REPO): done("zaid/zero-out-trees\n"),
            ("gh", "api", "repos/%s/git/ref/heads/main" % REPO): done(SHA + "\n"),
            ("gh", "api", "-X", "POST"): done("{}"),
            ("gh", "api", "-X", "PUT"): done("{}"),
            ("gh", "pr", "create"): done(PR + "\n"),
        }
        self.answers.update(answers or {})

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kw.append(kw)
        for n in (4, 3, 2):
            hit = self.answers.get(tuple(argv[:n]))
            if hit is not None and (n != 2 or True):
                if isinstance(hit, Exception):
                    raise hit
                return hit
        raise AssertionError(f"unexpected gh call {argv}")

    def names(self):
        return [" ".join(c[:4]) for c in self.calls]


@pytest.fixture(autouse=True)
def gh_present(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(github.time, "sleep", lambda s: None)


def test_push_rights_go_straight_to_a_branch_and_pr(tmp_path):
    gh = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url == PR and out.outbox is None and PR in out.message
    body = b'{"name": "Acme"}\n'
    branch = BRANCH
    assert gh.calls == [
        ["gh", "auth", "status"],
        ["gh", "api", "user", "--jq", ".login"],
        ["gh", "api", f"repos/{REPO}", "--jq", ".permissions.push"],
        ["gh", "api", f"repos/{REPO}/git/ref/heads/main", "--jq", ".object.sha"],
        ["gh", "api", "-X", "POST", f"repos/{REPO}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={SHA}"],
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/{REL}", "--input", "-"],
        ["gh", "pr", "create", "--repo", REPO, "--head", f"MAXIMUSSCORP:{branch}", "--base", "main",
         "--title", "Add Acme (8005550000)", "--body", gh.calls[-1][-1]],
    ]
    put = next(i for i, c in enumerate(gh.calls) if c[:4] == ["gh", "api", "-X", "PUT"])
    assert json.loads(gh.kw[put]["input"]) == {
        "message": f"Add {REL}", "content": base64.b64encode(body).decode(), "branch": branch}
    assert "input" not in gh.kw[put - 1] and base64.b64encode(body).decode() not in str(gh.calls)
    text = gh.calls[-1][-1]
    assert "3 observations" in text and "phone" in text and "crawl" in text and "Zero-Out" in text
    assert not list(tmp_path.iterdir())


def test_a_250_kb_file_goes_over_stdin_not_argv(tmp_path):
    body = b'{"x": "' + b"a" * (250 * 1024) + b'"}\n'
    gh = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n")})
    out = submit(contribution(body=body), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url == PR
    assert max(len(a) for c in gh.calls for a in c) < 1000  # no argument carries the file
    put = next(i for i, c in enumerate(gh.calls) if c[:4] == ["gh", "api", "-X", "PUT"])
    sent = json.loads(gh.kw[put]["input"])
    assert base64.b64decode(sent["content"]) == body


def test_no_shell_timeout_and_no_check(tmp_path):
    gh = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n")})
    submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    for kw in gh.kw:
        assert kw.get("shell") in (None, False) and kw["timeout"] == 60 and kw["check"] is False
        assert kw["capture_output"] is True and kw["text"] is True


def test_without_push_rights_it_forks_and_uses_the_forks_real_name(tmp_path):
    gh = FakeGh({("gh", "api", "repos/%s/forks" % REPO): done("zaid/renamed-fork\n")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url == PR
    assert ["gh", "repo", "fork", REPO, "--clone=false"] in gh.calls
    forks = next(c for c in gh.calls if c[2] == f"repos/{REPO}/forks")
    assert forks[3:] == ["--paginate", "--jq", '.[] | select(.owner.login == "zaid") | .full_name']
    post = next(c for c in gh.calls if c[:4] == ["gh", "api", "-X", "POST"])
    assert post[4] == "repos/zaid/renamed-fork/git/refs"
    put = next(c for c in gh.calls if c[:4] == ["gh", "api", "-X", "PUT"])
    assert put[4] == f"repos/zaid/renamed-fork/contents/{REL}"
    pr = gh.calls[-1]
    assert pr[pr.index("--repo") + 1] == REPO
    assert pr[pr.index("--head") + 1] == f"zaid:{BRANCH}"


def test_fork_creation_is_retried_inside_the_wait(tmp_path):
    forks = iter([done("", 0, ""), done("someone-else/x\n"), done("zaid/zero-out-trees\n"), done("zaid/zero-out-trees\n")])
    posts = iter([done("", 1, "Not Found"), done("{}")])
    gh = FakeGh()
    base = gh.__call__

    def call(argv, **kw):
        if argv[2] == f"repos/{REPO}/forks":
            gh.calls.append(list(argv))
            return next(forks)
        if argv[:4] == ["gh", "api", "-X", "POST"]:
            gh.calls.append(list(argv))
            return next(posts)
        return base(argv, **kw)

    out = submit(contribution(), REPO, run=call, outbox_dir=tmp_path)
    assert out.url == PR
    assert sum(1 for c in gh.calls if c[:4] == ["gh", "api", "-X", "POST"]) == 2


def test_fork_that_never_appears_falls_back_to_outbox(tmp_path, monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(github.time, "monotonic", lambda: next(clock))
    gh = FakeGh({("gh", "api", "repos/%s/forks" % REPO): done("", 0, "")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url is None and out.outbox == tmp_path / REL and out.outbox.read_bytes() == contribution().body
    assert "fork was not ready" in out.message


def test_pr_failure_after_the_file_is_up_says_where_it_is(tmp_path):
    gh = FakeGh({("gh", "pr", "create"): done("", 1, "pull request create failed: rate limited\nmore")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url is None and out.outbox is None and out.partial is True
    assert out.message == (f"The file is on your fork's branch {BRANCH} but the pull request was not opened: "
                           "pull request create failed: rate limited.")
    assert not list(tmp_path.iterdir())
    direct = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n"), ("gh", "pr", "create"): done("", 1, "nope")})
    out = submit(contribution(), REPO, run=direct, outbox_dir=tmp_path)
    assert out.partial and f"the branch {BRANCH} of {REPO} but" in out.message and "was not opened: nope." in out.message


def test_an_unreadable_pr_link_is_a_partial_result(tmp_path):
    gh = FakeGh({("gh", "pr", "create"): done("javascript:alert(1)\n")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url is None and out.partial and "pull request link" in out.message


def test_pr_body_does_not_claim_there_is_no_personal_data(tmp_path):
    gh = FakeGh()
    submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    body = gh.calls[-1][-1]
    assert "Personal data is redacted automatically; reviewers should still check for names." in body
    assert "no audio or personal data" not in body


def test_github_login_is_read_only_and_validated():
    gh = FakeGh()
    assert github.github_login(run=gh) == "zaid"
    assert gh.calls == [["gh", "api", "user", "--jq", ".login"]]
    assert github.github_login(run=FakeGh({("gh", "api", "user"): done("", 1, "no")})) is None
    assert github.github_login(run=FakeGh({("gh", "api", "user"): done("bad login\n")})) is None
    assert github.github_login(run=FakeGh({("gh", "api", "user"): OSError("x")})) is None


def test_github_login_is_none_without_gh(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda n: None)
    gh = FakeGh()
    assert github.github_login(run=gh) is None and gh.calls == []


def test_gh_missing_writes_the_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    gh = FakeGh()
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert gh.calls == []
    assert out.url is None and out.outbox == tmp_path / REL
    assert out.outbox.read_bytes() == b'{"name": "Acme"}\n'
    assert out.message == (
        f"GitHub CLI is not set up, so the file was saved to {out.outbox}. To share it, open "
        f"https://github.com/{REPO}/upload/main/trees/8005550000 and add the file.")


def test_auth_failure_writes_the_outbox(tmp_path):
    gh = FakeGh({("gh", "auth", "status"): done("", 1, "not logged in")})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert gh.calls == [["gh", "auth", "status"]]
    assert out.outbox == tmp_path / REL and out.message.startswith("GitHub CLI is not set up")


@pytest.mark.parametrize("fail", [
    ("gh", "api", "user"), ("gh", "api", "repos/%s/git/ref/heads/main" % REPO),
    ("gh", "api", "-X", "POST"), ("gh", "api", "-X", "PUT")])
def test_failing_step_falls_back_with_first_line_of_the_error(tmp_path, fail):
    err = "boom: first line\nsecond line" + "x" * 500
    gh = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n"), fail: done("", 1, err)})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url is None and out.outbox == tmp_path / REL and out.outbox.exists()
    assert "boom: first line" in out.message and "second line" not in out.message
    assert f"https://github.com/{REPO}/upload/main/trees/8005550000" in out.message


def test_long_error_line_is_cut(tmp_path):
    gh = FakeGh({("gh", "api", "user"): done("", 1, "e" * 900)})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert "e" * 200 in out.message and "e" * 201 not in out.message


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 60), FileNotFoundError("gh"), OSError("arg list too long")])
def test_runner_exceptions_fall_back(tmp_path, exc):
    gh = FakeGh({("gh", "api", "user"): exc})
    out = submit(contribution(), REPO, run=gh, outbox_dir=tmp_path)
    assert out.url is None and out.outbox == tmp_path / REL


def test_odd_outputs_fall_back(tmp_path):
    for answers in ({("gh", "api", "user"): done("bad login!\n")},
                    {("gh", "api", "repos/%s/git/ref/heads/main" % REPO): done("not-a-sha\n"),
                     ("gh", "api", "repos/%s" % REPO): done("true\n")},
                    ):
        out = submit(contribution(), REPO, run=FakeGh(answers), outbox_dir=tmp_path)
        assert out.url is None and out.outbox


@pytest.mark.parametrize("rel", [
    f"trees/800555000/2026-10-25-{FILE_ID}.json", f"trees/8005550000/../2026-10-25-{FILE_ID}.json",
    f"../trees/8005550000/2026-10-25-{FILE_ID}.json", f"trees/8005550000/2026-10-25-{FILE_ID}.txt",
    f"/trees/8005550000/2026-10-25-{FILE_ID}.json", f"trees/8005550000/2026-10-25-{FILE_ID} .json",
    "trees/8005550000/zaid-2026-10-25T10-00-00Z.json", "trees/8005550000/x.json",
    f"trees/8005550000/2026-10-25-{FILE_ID.upper()}.json", f"trees/8005550000/2026-10-25-{FILE_ID[:-1]}.json",
    f"trees/8005550000/2026-10-25T10-{FILE_ID}.json", f"trees/8005550000/2026-10-25-{FILE_ID}.json\n",
    f"trees/abcdefghij/2026-10-25-{FILE_ID}.json", f"other/8005550000/2026-10-25-{FILE_ID}.json"])
def test_bad_relpath_is_refused_before_anything_runs(tmp_path, rel):
    gh = FakeGh()
    with pytest.raises(ValueError):
        submit(contribution(rel), REPO, run=gh, outbox_dir=tmp_path)
    assert gh.calls == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize("repo", ["nodash", "a/b/c", "a/..", "a/b c", "a/b;ls", "a/b\nc"])
def test_bad_repo_is_refused(tmp_path, repo):
    with pytest.raises(ValueError):
        submit(contribution(), repo, run=FakeGh(), outbox_dir=tmp_path)


def test_file_id_must_match_the_file_name(tmp_path):
    c = contribution()
    c.file_id = "f" * 16
    with pytest.raises(ValueError):
        submit(c, REPO, run=FakeGh(), outbox_dir=tmp_path)


def test_hostile_company_name_is_cleaned_in_the_title(tmp_path):
    c = contribution()
    c.doc["name"] = "Acme\n--body x " + "y" * 300
    gh = FakeGh({("gh", "api", "repos/%s" % REPO): done("true\n")})
    submit(c, REPO, run=gh, outbox_dir=tmp_path)
    title = gh.calls[-1][gh.calls[-1].index("--title") + 1]
    assert "\n" not in title and len(title) < 160 and title.endswith("(8005550000)")


# ---------- download_trees ----------

def tarball(members, top="zero-out-trees-main"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, kind in members:
            info = tarfile.TarInfo(name if name.startswith("/") or name.startswith("..") else f"{top}/{name}")
            if kind == "file":
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                t.addfile(info)
            else:
                info.type = tarfile.DIRTYPE
                t.addfile(info)
    return buf.getvalue()


def fetcher(data):
    seen = []

    def fetch(url):
        seen.append(url)
        return io.BytesIO(data)

    fetch.seen = seen
    return fetch


def name(i, number="8005550000"):
    return f"trees/{number}/2026-10-25-{i:016x}.json"


GOOD = name(0xA)


def test_good_files_come_back_with_trees_relpaths():
    data = tarball([("README.md", b"hi", "file"), ("trees", b"", "dir"), (GOOD, b"{}", "file"),
                    (name(0xB, "8885550001"), b"[1]", "file")])
    f = fetcher(data)
    got = download_trees(REPO, fetch=f)
    assert got.files == [(GOOD, b"{}"), (name(0xB, "8885550001"), b"[1]")] and got.truncated is False
    assert f.seen == [f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/main"]


def test_symlinks_dotdot_and_odd_names_are_ignored():
    data = tarball([
        (name(1), b"", "symlink"), ("../evil/" + name(2), b"x", "file"),
        ("trees/8005550000/../9005550000/2026-10-25-0000000000000003.json", b"x", "file"), (name(4, "800555000"), b"x", "file"),
        (name(5) + ".txt", b"x", "file"), ("trees/8005550000/sub/2026-10-25-0000000000000006.json", b"x", "file"),
        ("other/8005550000/2026-10-25-0000000000000007.json", b"x", "file"), ("trees/8005550000/2026-10-25-000000000000 008.json", b"x", "file"), (GOOD, b"ok", "file")])
    assert download_trees(REPO, fetch=fetcher(data)).files == [(GOOD, b"ok")]


def test_oversize_members_are_skipped():
    data = tarball([(name(0x10), b"x" * (256 * 1024 + 1), "file"),
                    (name(0x11), b"y" * (256 * 1024), "file"), (GOOD, b"ok", "file")])
    out = download_trees(REPO, fetch=fetcher(data)).files
    assert [n for n, _ in out] == [name(0x11), GOOD]


def test_download_larger_than_the_cap_is_refused():
    data = tarball([(name(i), bytes([i]) * 20000, "file") for i in range(20)])
    with pytest.raises(RuntimeError, match="larger than allowed"):
        download_trees(REPO, fetch=fetcher(data), max_bytes=len(data) - 1)


def test_decompressed_total_is_capped_too():
    data = tarball([(name(i), b"0" * 100000, "file") for i in range(40)])
    assert len(data) < 40000  # compresses to almost nothing, so only the 4x total can stop it
    with pytest.raises(RuntimeError, match="larger than allowed"):
        download_trees(REPO, fetch=fetcher(data), max_bytes=len(data) + 10)


def test_max_files_stops_collecting():
    data = tarball([(name(i), b"{}", "file") for i in range(5)])
    got = download_trees(REPO, fetch=fetcher(data), max_files=3)
    assert [n for n, _ in got.files] == [name(i) for i in range(3)] and got.truncated is True
    exact = download_trees(REPO, fetch=fetcher(data), max_files=5)
    assert len(exact.files) == 5 and exact.truncated is False  # exactly full is not truncated
    skipped = tarball([(name(0), b"{}", "file"), ("README.md", b"x", "file")])
    assert download_trees(REPO, fetch=fetcher(skipped), max_files=1).truncated is False


def test_404_says_the_repo_was_not_found():
    def fetch(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(RuntimeError) as e:
        download_trees(REPO, fetch=fetch)
    assert str(e.value) == f"The community repo {REPO} was not found."


def test_other_failures_are_plain_sentences():
    def boom(url):
        raise urllib.error.URLError("dns")

    with pytest.raises(RuntimeError, match="Could not reach GitHub"):
        download_trees(REPO, fetch=boom)
    with pytest.raises(RuntimeError, match="not a valid archive"):
        download_trees(REPO, fetch=fetcher(b"this is not gzip at all"))
    with pytest.raises(ValueError):
        download_trees("bad repo", fetch=boom)


# ---------- redirects ----------

def test_a_redirect_is_an_error_sentence():
    def fetch(url):
        raise urllib.error.HTTPError(url, 302, "Found", {}, None)

    with pytest.raises(RuntimeError, match="does not follow redirects"):
        download_trees(REPO, fetch=fetch)


def test_default_fetch_never_follows_a_redirect(monkeypatch):
    handler = github._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/x") is None
    seen = {}

    class Opener:
        def open(self, url, timeout):
            seen["url"] = url
            raise urllib.error.HTTPError(url, 301, "Moved", {}, None)

    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(github.urllib.request, "build_opener", build)
    with pytest.raises(RuntimeError, match="redirect"):
        download_trees(REPO)
    assert seen["handlers"] == (github._NoRedirect,)
    assert seen["url"].startswith("https://codeload.github.com/")


def test_default_fetch_refuses_other_hosts():
    with pytest.raises(ValueError):
        github._fetch("https://evil.example/x.tar.gz")
    with pytest.raises(ValueError):
        github._fetch("http://codeload.github.com/x")
