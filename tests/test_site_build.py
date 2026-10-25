import itertools
import json
import socket
import sys
from datetime import date

import pytest

from zeroout import site_build


TODAY = date(2026, 10, 30)
_ids = itertools.count(1)


def obs(path="", text="Press 1 for billing", keys=("1",), heard="2026-10-01",
        outcome="menu", source="crawl", labels=None):
    return {
        "path": path, "prompt_text": text, "outcome": outcome, "prompt_seconds": 5.0,
        "heard_at": heard, "source": source,
        "options": [{"key": k, "label": (labels or {}).get(k, "opt" + k)} for k in keys],
    }


def put(trees, number, who, stamp, observations, name="Acme Wireless"):
    d = trees / number
    d.mkdir(parents=True, exist_ok=True)
    body = {"format": 1, "number": number, "name": name, "contributed_at": stamp,
            "observations": observations}
    p = d / f"{stamp}-{next(_ids):016x}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


@pytest.fixture
def dirs(tmp_path):
    t = tmp_path / "trees"
    t.mkdir()
    return t, tmp_path / "out"


def page(out, number):
    return (out / "c" / f"{number}.html").read_text(encoding="utf-8")


def test_latest_wins_and_counts(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01",
        [obs(text="Press 1 for billing.", heard="2026-10-01")])
    put(trees, "8885550199", "b", "2026-10-02",
        [obs(text="PRESS 1  for   billing", heard="2026-10-02")])
    put(trees, "8885550199", "c", "2026-10-03",
        [obs(text="Welcome. Press 1 for billing", heard="2026-10-03")])
    r = site_build.build(trees, out, today=TODAY)
    assert r["files"] == 3 and r["companies"] == 1 and r["nodes"] == 1 and r["invalid"] == {}
    html = page(out, "8885550199")
    assert "Welcome. Press 1 for billing" in html
    assert "heard 1" in html
    assert "changed since 2026-10-02" in html
    assert "last heard 2026-10-03" in html


def test_normalization_counts_equal(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01",
        [obs(text="Press 1 for billing.", heard="2026-10-01")])
    put(trees, "8885550199", "b", "2026-10-02",
        [obs(text="PRESS 1  for   billing", heard="2026-10-02")])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "heard 2" in html
    assert "changed since" not in html


def test_different_keys_count_as_changed(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(keys=("1",), heard="2026-10-01")])
    put(trees, "8885550199", "b", "2026-10-02", [obs(keys=("1", "2"), heard="2026-10-02")])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "heard 1" in html and "changed since 2026-10-01" in html


def test_nested_tree_and_source_counts(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs("", keys=("1",)),
        obs("1", text="Billing menu", keys=("2",), labels={}),
        obs("1.2", text="Agent", keys=(), outcome="human", source="phone"),
    ])
    r = site_build.build(trees, out, today=TODAY)
    assert r["nodes"] == 3
    html = page(out, "8885550199")
    assert html.count("<details") == 3
    assert "crawl 2" in html and "phone 1" in html
    assert "human" in html


def test_escaping_is_inert(dirs):
    trees, out = dirs
    evil = '<script>alert(1)</script>"onmouseover="x'
    put(trees, "8885550199", "a", "2026-10-01",
        [obs(text=evil, keys=("1",), labels={"1": evil}), obs("1", text=evil, keys=())],
        name=evil)
    site_build.build(trees, out, today=TODAY)
    for f in [out / "index.html", out / "c" / "8885550199.html"]:
        html = f.read_text(encoding="utf-8")
        assert "<script>alert" not in html
        assert '"onmouseover=' not in html
        assert "&lt;script&gt;" in html
        assert "javascript:" not in html


def test_csp_on_every_page_and_no_inline_or_external(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    put(trees, "8885550200", "a", "2026-10-01", [obs()], name="Beta")
    site_build.build(trees, out, today=TODAY)
    pages = list(out.rglob("*.html"))
    assert len(pages) == 3
    for p in pages:
        html = p.read_text(encoding="utf-8")
        assert ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
                "style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'\">") in html
        assert "http://" not in html and "https://" not in html
        assert "onclick" not in html and "<script>" not in html


def test_invalid_files_skipped_with_check(dirs):
    trees, out = dirs
    good = put(trees, "8885550199", "a", "2026-10-01", [obs()])
    bad = put(trees, "8885550200", "a", "2026-10-01", [obs()], name="Bad")

    def check(rel, body):
        return ["name not allowed"] if "8885550200" in rel else []

    r = site_build.build(trees, out, check=check, today=TODAY)
    assert r["files"] == 2 and r["companies"] == 1
    assert list(r["invalid"]) == [bad.relative_to(trees).as_posix()]
    assert r["invalid"][bad.relative_to(trees).as_posix()] == ["name not allowed"]
    assert not (out / "c" / "8885550200.html").exists()
    assert good.exists()


def test_unparseable_and_misplaced_files_are_invalid_without_check(dirs):
    trees, out = dirs
    (trees / "8885550199").mkdir()
    (trees / "8885550199" / "x-1.json").write_text("{nope", encoding="utf-8")
    (trees / "8885550201").mkdir()
    (trees / "8885550201" / "2026-10-01-00000000000000aa.json").write_text(
        json.dumps({"format": 1, "number": "../../etc", "name": "x", "observations": []}),
        encoding="utf-8")
    r = site_build.build(trees, out, today=TODAY)
    assert len(r["invalid"]) == 2 and r["companies"] == 0
    assert not list(out.glob("c/*")) or all(p.name != "etc.html" for p in out.rglob("*"))


def test_sorting_and_index(dirs):
    trees, out = dirs
    put(trees, "8885550300", "a", "2026-10-01", [obs(heard="2026-10-01")], name="zeta Co")
    put(trees, "8885550100", "a", "2026-10-01",
        [obs(heard="2026-10-05"), obs("1", keys=(), outcome="hold")], name="Alpha")
    put(trees, "8885550200", "a", "2026-10-01", [obs()], name="beta")
    site_build.build(trees, out, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.index("Alpha") < html.index("beta") < html.index("zeta Co")
    assert "2026-10-05" in html
    assert "site.js" in html and 'id="q"' in html
    assert (out / "site.js").is_file() and (out / "style.css").is_file()
    assert 'href="c/8885550100.html"' in html


def test_deterministic(dirs, tmp_path):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(), obs("1", keys=())])
    put(trees, "8885550200", "b", "2026-10-02", [obs()], name="B")
    site_build.build(trees, out, today=TODAY)
    out2 = tmp_path / "out2"
    site_build.build(trees, out2, today=TODAY)

    def snap(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    assert snap(out) == snap(out2) and len(snap(out)) == 5


def test_rebuild_clears_stale_pages(dirs):
    trees, out = dirs
    p = put(trees, "8885550199", "a", "2026-10-01", [obs()])
    site_build.build(trees, out, today=TODAY)
    p.unlink()
    site_build.build(trees, out, today=TODAY)
    assert not (out / "c" / "8885550199.html").exists()


def test_empty_trees(dirs):
    trees, out = dirs
    r = site_build.build(trees, out, today=TODAY)
    assert r == {"files": 0, "invalid": {}, "companies": 0, "nodes": 0}
    assert "No trees yet" in (out / "index.html").read_text(encoding="utf-8")


def test_no_network(dirs, monkeypatch):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    site_build.build(trees, out, today=TODAY)


def test_stdlib_only():
    import ast
    import sys
    src = open(site_build.__file__, encoding="utf-8").read()
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            assert n.level == 0
            names.add(n.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


def test_cli_exit_codes(dirs, capsys, monkeypatch):
    trees, out = dirs
    monkeypatch.setattr(site_build, "load_check", lambda: (lambda rel, body: []))
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    assert site_build.main([str(trees), str(out)]) == 0
    assert "companies" in capsys.readouterr().out
    (trees / "8885550199" / "bad.json").write_text("{", encoding="utf-8")
    assert site_build.main([str(trees), str(out)]) == 0
    printed = capsys.readouterr().out
    assert "bad.json" in printed and "invalid" in printed
    assert (out / "c" / "8885550199.html").exists()
    assert site_build.main([str(trees), str(out), "--strict"]) == 1


def test_cli_fails_when_checker_missing(dirs, capsys, monkeypatch):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    monkeypatch.setattr(site_build, "load_check", lambda: None)
    assert site_build.main([str(trees), str(out)]) == 2
    assert not out.exists()
    assert "community_check" in capsys.readouterr().out


def test_checker_loaded_from_sibling_file_not_sys_path(monkeypatch):
    from pathlib import Path
    import types
    decoy = types.ModuleType("community_check")
    decoy.check = lambda rel, body: ["decoy"]
    monkeypatch.setitem(sys.modules, "community_check", decoy)
    monkeypatch.setitem(sys.modules, "zeroout.community_check", decoy)
    check = site_build.load_check()
    sibling = Path(site_build.__file__).with_name("community_check.py")
    assert check is not decoy.check
    assert Path(check.__code__.co_filename) == sibling


def test_load_check_missing_sibling(monkeypatch, tmp_path):
    monkeypatch.setattr(site_build, "__file__", str(tmp_path / "site_build.py"))
    assert site_build.load_check() is None


def test_removed_branches_vanish(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs("", keys=("1", "2"), heard="2026-10-01"),
        obs("1", text="Billing", keys=("5",), heard="2026-10-01"),
        obs("1.5", text="Deep billing", keys=(), outcome="human", heard="2026-10-01"),
        obs("2", text="Support", keys=(), outcome="human", heard="2026-10-01"),
    ])
    put(trees, "8885550199", "b", "2026-10-05", [
        obs("", keys=("2",), heard="2026-10-05"),
    ])
    r = site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "Support" in html
    assert "Billing" not in html and "Deep billing" not in html
    assert r["nodes"] == 2


def test_child_needs_parent_option_even_if_deeper_is_listed(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs("", keys=("1",)),
        obs("1", text="Mid", keys=("2",)),
        obs("1.2", text="Leaf", keys=(), outcome="human"),
        obs("1.3", text="Orphan sibling", keys=(), outcome="human"),
        obs("7.7", text="Unlinked", keys=(), outcome="human"),
    ])
    r = site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "Leaf" in html and "Orphan sibling" not in html and "Unlinked" not in html
    assert r["nodes"] == 3


def test_missing_root_shows_no_nodes(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs("1", keys=())])
    r = site_build.build(trees, out, today=TODAY)
    assert r["nodes"] == 0 and r["companies"] == 1
    assert "<details" not in page(out, "8885550199")


def test_future_heard_at_ignored(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(text="Real menu", heard="2026-10-01")])
    put(trees, "8885550199", "b", "2026-10-02", [obs(text="Planted menu", heard="2099-01-01")])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "Real menu" in html and "Planted" not in html and "2099" not in html
    assert "changed since" not in html


def test_future_heard_at_allowed_when_today_moves(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(text="Later menu", heard="2026-11-02")])
    site_build.build(trees, out, today=date(2026, 11, 2))
    assert "Later menu" in page(out, "8885550199")


def test_today_defaults_to_utc_today(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(text="Planted", heard="2999-01-01"), obs("1", keys=(), heard="2026-10-01")])
    r = site_build.build(trees, out)
    assert r["companies"] == 1 and "Planted" not in page(out, "8885550199")


def test_name_is_most_common_not_newest(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()], name="Acme")
    put(trees, "8885550199", "b", "2026-10-02", [obs()], name="Acme")
    put(trees, "8885550199", "c", "2026-10-03", [obs()], name="Evil Corp")
    site_build.build(trees, out, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Acme" in html and "Evil Corp" not in html


def test_symlinked_files_skipped(dirs, tmp_path):
    trees, out = dirs
    real = put(trees, "8885550199", "a", "2026-10-01", [obs()])
    elsewhere = tmp_path / "secret.json"
    elsewhere.write_text(real.read_text(encoding="utf-8").replace("Acme Wireless", "Linked Co"), encoding="utf-8")
    (trees / "8885550199" / "2026-10-02-00000000000000ff.json").symlink_to(elsewhere)
    (trees / "8885550300").symlink_to(elsewhere.parent, target_is_directory=True)
    r = site_build.build(trees, out, today=TODAY)
    assert "Linked Co" not in (out / "index.html").read_text(encoding="utf-8")
    assert any("symlink" in e[0] for e in r["invalid"].values())
