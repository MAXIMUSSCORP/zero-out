"""Share a mapped tree with the community repo, and fetch everyone else's.

Submitting uses the GitHub CLI when it is set up and falls back to a file in the outbox folder.
Downloading reads the repo's tarball as a stream and never writes it to disk.
"""
import base64
import gzip
import json
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

from .config import valid_repo

GH_TIMEOUT = 60
FORK_WAIT = 30
MAX_MEMBER = 256 * 1024
RELPATH_RE = re.compile(r"trees/([0-9]{10})/([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9a-f]{16})\.json")
LOGIN_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
PR_RE = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/pull/[0-9]+")
BRANCH_RE = re.compile(r"contrib/[0-9]{10}-[0-9a-f]{16}")


@dataclass
class Download:
    files: list[tuple[str, bytes]]
    truncated: bool  # more valid files were there than max_files


@dataclass
class Submitted:
    url: str | None
    outbox: Path | None
    message: str
    # The file is public on a branch but no pull request was opened.
    partial: bool = False


class CommunityError(RuntimeError):
    """A download problem with a message fit to show the person as it is."""


class StepFailed(Exception):
    pass


def _check_repo(repo: str) -> None:
    if not valid_repo(repo) or ".." in repo:
        raise ValueError(f"{repo!r} is not a GitHub repo name like owner/name.")


def _check_relpath(rel: str) -> re.Match:
    m = RELPATH_RE.fullmatch(rel)
    if m is None or ".." in rel:
        raise ValueError(f"{rel!r} is not a valid community file path.")
    return m


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0][:200] if (text or "").strip() else "no details"


def _clean_name(name) -> str:
    text = re.sub(r"[^\x20-\x7e]", " ", str(name or "")).strip()
    return re.sub(r"\s+", " ", text)[:60] or "Unnamed company"


def _sources(doc: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in doc.get("observations", []) if isinstance(doc, dict) else []:
        src = o.get("source") if isinstance(o, dict) else None
        if isinstance(src, str) and re.fullmatch(r"[a-z_]{1,20}", src):
            counts[src] = counts.get(src, 0) + 1
    return counts


def _company_name(doc: dict) -> str:
    return _clean_name(doc.get("name") if isinstance(doc, dict) else None)


def _outbox(contribution, repo: str, outbox_dir: Path, why: str) -> Submitted:
    number = _check_relpath(contribution.relpath).group(1)
    dest = Path(outbox_dir) / contribution.relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(contribution.body)
    return Submitted(None, dest, (
        f"{why}, so the file was saved to {dest}. To share it, open "
        f"https://github.com/{repo}/upload/main/trees/{number} and add the file."))


def github_login(run=subprocess.run) -> str | None:
    """Who a share would be made as. Read only; None when gh is missing or not signed in."""
    if shutil.which("gh") is None:
        return None
    try:
        r = run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True,
                timeout=GH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    login = _scalar((r.stdout or "").strip()) if r.returncode == 0 else ""
    return login if LOGIN_RE.fullmatch(login) else None


def submit(contribution, repo: str, *, run=subprocess.run, outbox_dir: Path,
           sleep=None, clock=None) -> Submitted:
    _check_repo(repo)
    m = _check_relpath(contribution.relpath)
    number, file_id = m.group(1), m.group(3)
    if getattr(contribution, "file_id", file_id) != file_id:
        raise ValueError("The file id does not match the file name.")
    branch = f"contrib/{number}-{file_id}"
    if not BRANCH_RE.fullmatch(branch):
        raise ValueError(f"{branch!r} is not a valid branch name.")
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    unset = "GitHub CLI is not set up"
    if shutil.which("gh") is None:
        return _outbox(contribution, repo, outbox_dir, unset)

    def gh(*argv: str, stdin: str | None = None) -> str:
        extra = {} if stdin is None else {"input": stdin}
        try:
            r = run(["gh", *argv], capture_output=True, text=True, timeout=GH_TIMEOUT, check=False, **extra)
        except (OSError, subprocess.SubprocessError) as e:
            raise StepFailed(_first_line(str(e)))
        if r.returncode != 0:
            raise StepFailed(_first_line(r.stderr or r.stdout))
        return (r.stdout or "").strip()

    try:
        gh("auth", "status")
    except StepFailed:
        return _outbox(contribution, repo, outbox_dir, unset)
    target = repo
    try:
        login = _scalar(gh("api", "user", "--jq", ".login"))
        if not LOGIN_RE.fullmatch(login):
            raise StepFailed("GitHub did not return a usable user name")
        forking = _scalar(gh("api", f"repos/{repo}", "--jq", ".permissions.push")).lower() != "true"
        if forking:
            gh("repo", "fork", repo, "--clone=false")
        sha = _scalar(gh("api", f"repos/{repo}/git/ref/heads/main", "--jq", ".object.sha"))
        if not SHA_RE.fullmatch(sha):
            raise StepFailed("GitHub did not return a usable commit")

        def make_branch(where: str) -> None:
            gh("api", "-X", "POST", f"repos/{where}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")

        if not forking:
            make_branch(repo)
        else:
            # A new fork can take a few seconds to show up and accept a branch.
            start, found = clock(), None
            while True:
                try:
                    if found is None:
                        found = _fork_name(gh, repo, login)
                    make_branch(found)
                    break
                except StepFailed as e:
                    if clock() - start >= FORK_WAIT:
                        raise StepFailed(f"the fork was not ready in time: {e}")
                    sleep(2)
            target = found
        # The file goes over stdin: one argument is capped near 128 KB, and files may be 256 KB.
        gh("api", "-X", "PUT", f"repos/{target}/contents/{contribution.relpath}", "--input", "-",
           stdin=json.dumps({"message": f"Add {contribution.relpath}",
                             "content": base64.b64encode(contribution.body).decode("ascii"),
                             "branch": branch}))
    except StepFailed as e:
        return _outbox(contribution, repo, outbox_dir, f"GitHub could not take the file ({e})")
    count = len(contribution.observation_ids)
    sources = ", ".join(f"{k} {v}" for k, v in sorted(_sources(contribution.doc).items())) or "not recorded"
    body = (f"{count} observation{'' if count == 1 else 's'} for {number}. Sources: {sources}. "
            "Made by Zero-Out from the contributor's own mapping. Personal data is redacted automatically; "
            "reviewers should still check for names.")
    try:
        out = gh("pr", "create", "--repo", repo, "--head", f"{target.split('/')[0]}:{branch}", "--base", "main",
                 "--title", f"Add {_company_name(contribution.doc)} ({number})", "--body", body)
        url = out.splitlines()[-1].strip() if out else ""
        if not PR_RE.fullmatch(url):
            raise StepFailed("GitHub did not return a pull request link")
    except StepFailed as e:
        where = f"your fork's branch {branch}" if target != repo else f"the branch {branch} of {repo}"
        return Submitted(None, None, (
            f"The file is on {where} but the pull request was not opened: {e}."), partial=True)
    return Submitted(url, None, f"Opened a pull request: {url}")


def _fork_name(gh, repo: str, login: str) -> str:
    out = gh("api", f"repos/{repo}/forks", "--paginate", "--jq",
             f'.[] | select(.owner.login == "{login}") | .full_name')
    name = out.splitlines()[0].strip() if out else ""
    if not valid_repo(name) or name.split("/")[0].lower() != login.lower():
        raise StepFailed("your fork has not appeared yet")
    return name


def _scalar(text: str) -> str:
    try:
        value = json.loads(text)
    except ValueError:
        return text.strip()
    return str(value).strip() if isinstance(value, (str, bool, int)) else text.strip()


# ---------- download ----------

class _Capped:
    """A file object that refuses to hand out more than `limit` bytes."""

    def __init__(self, raw, limit: int):
        self.raw, self.left = raw, limit

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n if n is not None and n >= 0 else self.left + 1)
        self.left -= len(data)
        if self.left < 0:
            raise CommunityError("The community download is larger than allowed.")
        return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # urllib then raises HTTPError with the 3xx code


def _fetch(url: str):
    if not url.startswith("https://codeload.github.com/"):
        raise ValueError("Only downloads from codeload.github.com are allowed.")
    return urllib.request.build_opener(_NoRedirect).open(url, timeout=GH_TIMEOUT)


def download_trees(repo: str, *, fetch=None, max_bytes: int = 50 * 1024 * 1024,
                   max_files: int = 20000) -> Download:
    _check_repo(repo)
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/main"
    try:
        raw = (fetch or _fetch)(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise CommunityError(f"The community repo {repo} was not found.")
        if 300 <= e.code < 400:
            raise CommunityError("GitHub redirected the community download, and Zero-Out does not follow redirects.")
        raise CommunityError(f"GitHub answered {e.code} for the community repo {repo}.")
    except (urllib.error.URLError, OSError):
        raise CommunityError("Could not reach GitHub. Check the internet connection and try again.")
    files: list[tuple[str, bytes]] = []
    truncated = False
    try:
        with raw:
            packed = _Capped(raw, max_bytes)
            plain = _Capped(gzip.GzipFile(fileobj=packed, mode="rb"), 4 * max_bytes)
            with tarfile.open(fileobj=plain, mode="r|") as tar:
                for member in tar:
                    rel = _member_path(member)
                    if rel is None:
                        continue
                    if len(files) >= max_files:
                        truncated = True
                        break
                    data = tar.extractfile(member).read(MAX_MEMBER + 1)
                    files.append((rel, data))
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error, OSError):
        raise CommunityError("The community download was not a valid archive.")
    return Download(files, truncated)


def _member_path(member) -> str | None:
    if not member.isreg() or member.size > MAX_MEMBER:
        return None
    _, _, rel = member.name.partition("/")
    if ".." in rel or not RELPATH_RE.fullmatch(rel):
        return None
    return rel
