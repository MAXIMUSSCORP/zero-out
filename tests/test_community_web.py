import sys
from types import SimpleNamespace

import pytest

import zeroout
from tests.test_phone_web import local_client
from tests.test_web import env, web  # noqa: F401
from zeroout import github
from zeroout.github import Submitted

OK_ORIGIN = {"Origin": "http://127.0.0.1:8000"}
EVIL = {"Origin": "http://evil.example"}
PR = "https://github.com/MAXIMUSSCORP/zero-out-trees/pull/9"
PUBLIC = ("This opens a public pull request on MAXIMUSSCORP/zero-out-trees as @zaid. The file is public as soon "
          "as you share it, before anyone reviews it, and GitHub keeps it even if the pull request is closed.")


class FakeCommunity:
    def __init__(self):
        self.ids = [4, 5, 6]
        self.builds = 0
        self.nothing = False
        self.marked, self.imported = [], []
        self.report = SimpleNamespace(files=12, invalid=["trees/8005550000/bad.json", "<i>x</i>"],
                                      observations_added=30, already_had=5, offered=3, truncated=False,
                                      capped=False, orphans=0, future=0, results={})
        self.offers = [{"number": "8885550001", "name": "Evil <b>Bank</b>", "files": 2,
                        "first_seen": "2026-10-01", "last_seen": "2026-10-20"}]
        self.import_exc = None
        self.offer_limits = []

    def build_contribution(self, conn, company_id, *, now=None):
        if self.nothing:
            return None
        self.builds += 1
        fid = f"{self.builds:016x}"
        return SimpleNamespace(
            relpath=f"trees/8005550000/2026-10-25-{fid}.json", file_id=fid,
            doc={"name": "Acme", "number": "8005550000",
                 "observations": [{"source": "phone"}, {"source": "phone"}, {"source": "crawl"}]},
            body=f'{{"name": "Acme <b>x</b>", "build": {self.builds}}}\n'.encode(), observation_ids=list(self.ids))

    def mark_contributed(self, conn, ids, when):
        self.marked.append((list(ids), when))

    def import_files(self, conn, files, *, same_meaning, max_model_checks=200, truncated=False):
        if self.import_exc:
            raise self.import_exc
        self.imported.append(list(files))
        self.report.truncated = truncated
        return self.report

    def list_offers(self, conn, limit=None):
        self.offer_limits.append(limit)
        return self.offers


@pytest.fixture
def comm(monkeypatch):
    f = FakeCommunity()
    monkeypatch.setitem(sys.modules, "zeroout.community", f)
    monkeypatch.setattr(zeroout, "community", f, raising=False)
    f.submitted = []
    f.real_download = github.download_trees

    def submit(c, repo, *, outbox_dir, **kw):
        f.submitted.append((c, repo, outbox_dir))
        return Submitted(PR, None, f"Opened a pull request: {PR}")

    monkeypatch.setattr(github, "submit", submit)
    monkeypatch.setattr(github, "github_login", lambda **kw: "zaid")
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: github.Download([("trees/8005550000/a.json", b"{}")], False))
    return f


def post(c, path, headers=OK_ORIGIN, **kw):
    return c.post(path, headers=headers, **kw)


def preview(c, env):
    return post(c, f"/c/{env.cid}/contribute/preview")


def share(c, env):
    return post(c, f"/c/{env.cid}/contribute")


def test_hosted_404(env, comm):
    for path in (f"/c/{env.cid}/contribute/preview", f"/c/{env.cid}/contribute", "/community/update"):
        assert env.client.post(path).status_code == 404, path
    assert "Share with the community" not in env.client.get(f"/c/{env.cid}").text
    page = env.client.get("/").text
    assert "Update from community" not in page and "Offered by the community" not in page


def test_local_pages_show_the_buttons(env, comm):
    c = local_client(env)
    assert "Share with the community" in c.get(f"/c/{env.cid}").text
    assert "Update from community" in c.get("/").text


@pytest.mark.parametrize("path", ["/c/1/contribute/preview", "/c/1/contribute", "/community/update"])
def test_foreign_origin_is_403(env, comm, path):
    c = local_client(env)
    assert post(c, path, headers=EVIL).status_code == 403
    assert comm.marked == [] and comm.imported == []


# ---------- preview and confirm ----------

def test_preview_says_it_is_public_and_names_the_account_and_company(env, comm):
    r = preview(local_client(env), env)
    assert r.status_code == 200
    assert PUBLIC in r.text
    assert "Acme (8005550000)" in r.text
    assert "3 observations from this computer are ready to share" in r.text
    assert "phone 2" in r.text and "crawl 1" in r.text
    assert "Acme &lt;b&gt;x&lt;/b&gt;" in r.text and "<b>x</b>" not in r.text
    assert f'hx-post="/c/{env.cid}/contribute"' in r.text and "Share</button>" in r.text
    assert comm.marked == [] and comm.submitted == []


def test_preview_without_a_github_account_says_nothing_will_be_posted(env, comm, monkeypatch):
    monkeypatch.setattr(github, "github_login", lambda **kw: None)
    r = preview(local_client(env), env)
    assert "GitHub CLI is not set up" in r.text and "outbox" in r.text and "public pull request" not in r.text


def test_preview_nothing_to_share(env, comm):
    comm.nothing = True
    r = preview(local_client(env), env)
    assert "Nothing new to share for this company." in r.text and "Share</button>" not in r.text


def test_preview_unknown_company(env, comm):
    assert preview(local_client(env), SimpleNamespace(cid=999)).status_code == 404


def test_share_without_a_preview_is_refused(env, comm):
    r = share(local_client(env), env)
    assert r.status_code == 409 and "Preview again" in r.text
    assert comm.submitted == [] and comm.marked == []


def test_share_sends_exactly_the_previewed_body_and_marks_only_its_ids(env, comm):
    c = local_client(env)
    shown = preview(c, env)
    assert "&#34;build&#34;: 1" in shown.text
    r = share(c, env)
    assert r.status_code == 200 and f'<a href="{PR}"' in r.text
    sent = comm.submitted[0][0]
    assert b'"build": 1' in sent.body and sent.observation_ids == [4, 5, 6]
    assert comm.builds == 2  # rebuilt only to compare ids
    assert len(comm.marked) == 1 and comm.marked[0][0] == [4, 5, 6] and comm.marked[0][1]
    assert share(c, env).status_code == 409  # a preview is used once


def test_share_when_something_new_was_mapped_shows_the_new_preview_instead(env, comm):
    c = local_client(env)
    preview(c, env)
    comm.ids = [4, 5, 6, 7]
    r = share(c, env)
    assert r.status_code == 200
    assert "Something new was mapped since the preview; check this version." in r.text
    assert "4 observations from this computer are ready to share" in r.text and "Share</button>" in r.text
    assert comm.submitted == [] and comm.marked == []
    r = share(c, env)  # confirming the new preview shares it
    assert comm.submitted[0][0].observation_ids == [4, 5, 6, 7] and comm.marked[0][0] == [4, 5, 6, 7]


def test_share_when_everything_was_shared_meanwhile(env, comm):
    c = local_client(env)
    preview(c, env)
    comm.nothing = True
    assert "Nothing new to share" in share(c, env).text
    assert comm.submitted == []


def test_preview_expires_after_fifteen_minutes(env, comm, monkeypatch):
    c = local_client(env)
    preview(c, env)
    real = web.time.monotonic()
    monkeypatch.setattr(web.time, "monotonic", lambda: real + 901)
    assert share(c, env).status_code == 409 and comm.submitted == []


def test_a_preview_belongs_to_its_company(env, comm):
    c = local_client(env)
    other = env.conn.execute("INSERT INTO companies (name, number) VALUES ('B', '+18005550001')").lastrowid
    preview(c, env)
    assert post(c, f"/c/{other}/contribute").status_code == 409 and comm.submitted == []


def test_share_to_outbox_marks_and_says_so(env, comm, monkeypatch, tmp_path):
    seen = {}

    def fake(c, repo, *, outbox_dir, **kw):
        seen.update(repo=repo, outbox=outbox_dir)
        return Submitted(None, tmp_path / "x.json", "GitHub CLI is not set up, so the file was saved to /x. To share it, open https://github.com/a/b/upload/main/trees/8005550000 and add the file.")

    monkeypatch.setattr(github, "submit", fake)
    c = local_client(env)
    preview(c, env)
    r = share(c, env)
    assert "GitHub CLI is not set up" in r.text and "marked as shared" in r.text
    assert comm.marked[0][0] == [4, 5, 6]
    assert seen["repo"] == env.st.community_repo and seen["outbox"].name == "outbox"


def test_partial_result_is_shown_and_marked(env, comm, monkeypatch):
    msg = "The file is on your fork's branch contrib/8005550000-0000000000000001 but the pull request was not opened: rate limited."
    monkeypatch.setattr(github, "submit", lambda *a, **k: Submitted(None, None, msg, partial=True))
    c = local_client(env)
    preview(c, env)
    r = share(c, env)
    assert msg.replace("'", "&#39;") in r.text and comm.marked[0][0] == [4, 5, 6]


def test_share_failure_marks_nothing(env, comm, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(github, "submit", boom)
    c = local_client(env)
    preview(c, env)
    r = share(c, env)
    assert "Something went wrong sharing" in r.text and "disk full" not in r.text
    assert comm.marked == []


def test_share_refused_while_crawl_or_phone_clip_runs(env, comm):
    c = local_client(env)
    preview(c, env)
    c.app_obj.state.running.add(env.cid)
    r = share(c, env)
    assert r.status_code == 409 and "Already mapping this company" in r.text and comm.marked == []
    c.app_obj.state.running.discard(env.cid)
    c.app_obj.state.phone["busy"].add(env.cid)
    assert share(c, env).status_code == 409
    c.app_obj.state.phone["busy"].discard(env.cid)
    assert share(c, env).status_code == 200  # the refused tries did not use up the preview


def test_share_is_single_flight_per_company(env, comm, monkeypatch):
    c = local_client(env)
    seen = {}

    def slow(contribution, repo, *, outbox_dir, **kw):
        seen["second"] = share(c, env)
        return Submitted(PR, None, "ok")

    monkeypatch.setattr(github, "submit", slow)
    preview(c, env)
    assert share(c, env).status_code == 200
    assert seen["second"].status_code == 409


def test_share_rate_limit(env, comm, monkeypatch):
    monkeypatch.setattr(web, "CONTRIBUTE_PER_HOUR", 2)
    c = local_client(env)
    codes = []
    for _ in range(3):
        preview(c, env)
        codes.append(share(c, env).status_code)
    assert codes == [200, 200, 429]


# ---------- update and offers ----------

def test_update_reports_and_lists_invalid_paths(env, comm):
    r = post(local_client(env), "/community/update")
    assert r.status_code == 200
    assert "Checked 12 files: added 30 observations, already had 5, skipped 2 invalid files." in r.text
    assert "3 numbers offered by the community" in r.text
    assert "trees/8005550000/bad.json" in r.text and "&lt;i&gt;x&lt;/i&gt;" in r.text and "<i>x</i>" not in r.text
    assert comm.imported == [[("trees/8005550000/a.json", b"{}")]]
    assert "more files than one update reads" not in r.text


def test_update_says_when_the_download_was_truncated(env, comm, monkeypatch):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for i in range(4):
            data = b"{}"
            info = tarfile.TarInfo(f"repo-main/trees/8005550000/2026-10-25-{i:016x}.json")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    real = comm.real_download
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: real(
        repo, fetch=lambda url: io.BytesIO(buf.getvalue()), max_files=2))
    r = post(local_client(env), "/community/update")
    assert "The community repo has more files than one update reads; some were not checked." in r.text
    assert len(comm.imported[0]) == 2
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: real(
        repo, fetch=lambda url: io.BytesIO(buf.getvalue()), max_files=4))
    assert "more files than one update reads" not in post(local_client(env), "/community/update").text


def test_update_says_when_companies_were_capped_and_orphans_skipped(env, comm):
    comm.report.capped, comm.report.orphans = True, 7
    t = post(local_client(env), "/community/update").text
    assert "Some companies have more community data than one update brings in; run Update again later." in t
    assert "Skipped 7 observations for menu options your tree does not have." in t
    comm.report.capped, comm.report.orphans = False, 0
    t = post(local_client(env), "/community/update").text
    assert "run Update again later" not in t and "menu options your tree does not have" not in t
    comm.report.orphans = 1
    assert "Skipped 1 observation for menu options" in post(local_client(env), "/community/update").text


def test_update_says_how_many_future_dated_observations_were_skipped(env, comm):
    assert "dated in the future" not in post(local_client(env), "/community/update").text
    comm.report.future = 3
    assert "Skipped 3 observations dated in the future." in post(local_client(env), "/community/update").text
    comm.report.future = 1
    assert "Skipped 1 observation dated in the future." in post(local_client(env), "/community/update").text


def test_offers_list_asks_for_at_most_50(env, comm):
    local_client(env).get("/")
    assert comm.offer_limits and set(comm.offer_limits) == {50}


def test_update_refreshes_the_offers_list_out_of_band(env, comm):
    r = post(local_client(env), "/community/update")
    assert 'id="offers"' in r.text and 'hx-swap-oob="true"' in r.text and "8885550001" in r.text


def test_update_lists_at_most_ten_invalid(env, comm):
    comm.report.invalid = [f"trees/8005550000/bad{i}.json" for i in range(25)]
    r = post(local_client(env), "/community/update")
    assert "bad9.json" in r.text and "bad10.json" not in r.text and "skipped 25 invalid files" in r.text


def test_update_uses_the_configured_repo(env, comm, monkeypatch):
    seen = []
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: seen.append(repo) or github.Download([], False))
    post(local_client(env), "/community/update")
    assert seen == [env.st.community_repo]


def test_update_errors_are_plain_sentences(env, comm, monkeypatch):
    def missing(repo, **kw):
        raise github.CommunityError(f"The community repo {repo} was not found.")

    monkeypatch.setattr(github, "download_trees", missing)
    r = post(local_client(env), "/community/update")
    assert "The community repo MAXIMUSSCORP/zero-out-trees was not found." in r.text
    comm.import_exc = KeyError("secret")
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: github.Download([], False))
    r = post(local_client(env), "/community/update")
    assert "Something went wrong updating from the community" in r.text and "secret" not in r.text


def test_update_redirect_error_is_shown(env, comm, monkeypatch):
    def redirected(repo, **kw):
        raise github.CommunityError("GitHub redirected the community download, and Zero-Out does not follow redirects.")

    monkeypatch.setattr(github, "download_trees", redirected)
    assert "does not follow redirects" in post(local_client(env), "/community/update").text


def test_update_single_flight_and_rate_limit(env, comm, monkeypatch):
    c = local_client(env)
    seen = {}

    def slow(repo, **kw):
        seen["second"] = post(c, "/community/update")
        return github.Download([], False)

    monkeypatch.setattr(github, "download_trees", slow)
    assert post(c, "/community/update").status_code == 200
    assert seen["second"].status_code == 409
    monkeypatch.setattr(github, "download_trees", lambda repo, **kw: github.Download([], False))
    monkeypatch.setattr(web, "UPDATE_PER_HOUR", 1)
    c2 = local_client(env)
    assert [post(c2, "/community/update").status_code for _ in range(2)] == [200, 429]


def test_offers_are_listed_with_a_warning_and_an_editable_add_form(env, comm):
    page = local_client(env).get("/").text
    assert "Offered by the community" in page
    assert ("These numbers come from other people and are not verified. Check a number against your card, "
            "statement or the company's website before you add it.") in page
    assert "8885550001" in page and "name given by a contributor: Evil &lt;b&gt;Bank&lt;/b&gt;" in page
    assert "<b>Bank</b>" not in page
    assert "2 files" in page and "2026-10-01" in page and "2026-10-20" in page
    assert 'action="/companies"' in page and 'value="Evil &lt;b&gt;Bank&lt;/b&gt;"' in page
    assert 'name="number"' in page and 'value="8885550001"' in page and ">Add</button>" in page


def test_no_offers_no_section(env, comm):
    comm.offers = []
    assert "Offered by the community" not in local_client(env).get("/").text


def test_an_offer_can_be_added_through_the_existing_route(env, comm):
    c = local_client(env)
    r = c.post("/companies", data={"name": "Real Bank", "number": "8885550001"}, headers=OK_ORIGIN,
               follow_redirects=False)
    assert r.status_code == 303
    row = env.conn.execute("SELECT name FROM companies WHERE number = '+18885550001'").fetchone()
    assert row["name"] == "Real Bank"
