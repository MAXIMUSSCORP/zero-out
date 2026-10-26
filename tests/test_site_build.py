import base64
import hashlib
import itertools
import json
import re
import socket
import sys
from datetime import date
from pathlib import Path

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
    assert '<li><span class="t">Welcome.</span></li>' in html
    assert '<li><span class="t">Press 1 for billing</span></li>' in html
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


def test_csp_on_every_page_and_no_external(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    put(trees, "8885550200", "a", "2026-10-01", [obs()], name="Beta")
    site_build.build(trees, out, today=TODAY)
    pages = list(out.rglob("*.html"))
    assert len(pages) == 3
    digest = base64.b64encode(hashlib.sha256(site_build.CSS.encode("utf-8")).digest()).decode()
    for p in pages:
        html = p.read_text(encoding="utf-8")
        assert ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
                f"style-src 'sha256-{digest}'; script-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'\">") in html
        assert html.count("<style>") == 1 and f"<style>{site_build.CSS}</style>" in html
        assert "style=" not in html and 'rel="stylesheet"' not in html
        assert "http://" not in html and "https://" not in html
        assert "onclick" not in html and "<script>" not in html and "<form" not in html
    assert not (out / "style.css").exists()


def test_inline_style_is_covered_by_the_csp_hash_whatever_the_css_holds():
    assert "</style" not in site_build.CSS.lower()
    assert "unsafe-inline" not in site_build.CSP and "'self'" in site_build.CSP


def test_look_tokens_are_datums_light_and_dark():
    css = site_build.CSS
    datum = (Path("/home/zaidv/datum/src/styles.css"))
    for tok in ("--g1:#FCFCFC", "--g12:#1B1B1B", "--g1:#0A0A0A", "--g12:#EFEFEF", "--inv-bg:#000", "--inv-bg:#FFF"):
        assert tok in css
    if datum.is_file():
        want = re.findall(r"--g\d+:#[0-9A-F]{6}", datum.read_text(encoding="utf-8")[:1800])
        assert want and all(w in css for w in want)
    assert "prefers-color-scheme:dark" in css and 'data-theme="dark"' in css
    assert "Inter," in css and "980px" in css and "999px" not in css
    assert "monospace" not in css


def test_prompt_is_a_quiet_quote_and_key_caps_only_in_options(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs(text="Thanks for calling. For billing press 1, for support press 2.", keys=("1", "2"),
            labels={"1": "Billing", "2": "Support"}),
        obs("2", text="Support. Press star to repeat.", keys=("*",), labels={"*": "Repeat"}),
    ])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert html.count('<div class="said"><p class="lbl">What the menu says</p><ol class="lines">') == 2
    assert '<li><span class="t">Thanks for calling.</span></li>' in html
    assert '<li><span class="t">For billing press 1</span></li>' in html
    assert '<li><span class="t">for support press 2.</span></li>' in html
    for quote in re.findall(r'<ol class="lines">.*?</ol>', html):
        assert "key" not in quote
    assert '<li><span class="key sm">1</span><span class="t">Billing</span></li>' in html
    assert ('<li><span class="key sm">*</span><span class="t">Repeat</span>'
            ' <span class="state">Replays a menu</span></li>') in html
    assert '<span class="key lit">2</span>' in html
    assert ".said{" in site_build.CSS and "border-left:2px" in site_build.CSS
    assert html.count('class="note"') == 1 and "read only" in html and "nothing on it is verified" in html


def test_options_sort_1_to_9_then_0_then_star_pound(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs(keys=("#", "0", "*", "9", "2", "10", "1", "x"), labels={k: "L" + k for k in "#0*92101x"} | {"10": "Lten"})])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    order = [html.index(f'<span class="key sm">{k}</span>') for k in ("1", "2", "9", "0", "*", "#", "10", "x")]
    assert order == sorted(order)


@pytest.mark.parametrize("field", ["outcome", "status"])
def test_voice_menu_says_so_and_lists_no_keys(dirs, field):
    trees, out = dirs
    o = obs(text="Tell me what you need.", keys=("1",), outcome="menu")
    o[field] = "speech_menu"
    put(trees, "8885550199", "a", "2026-10-01", [o])
    put(trees, "8885550200", "a", "2026-10-01", [obs()], name="Beta")
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "Voice menu: you say what you need; there are no keys to press." in html
    assert '<ul class="opts">' not in html and '<span class="key sm">' not in html
    assert "Voice menu" not in page(out, "8885550200")
    idx = (out / "index.html").read_text(encoding="utf-8")
    row = re.search(r'<li data-search="[^"]*8885550199.*?</li>', idx).group(0)
    assert "Voice menu, no keys" in row
    assert "Voice menu" not in re.search(r'<li data-search="[^"]*8885550200.*?</li>', idx).group(0)


def test_index_is_a_list_of_numbers_with_a_note(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    site_build.build(trees, out, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<ul class="list companies">' in html and "8885550199 · 1 menus" in html
    assert 'class="note"' in html and "read only" in html


def test_only_options_shown_count_as_keys(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs(text="Press 1 for billing. Press 7 for fun.", keys=("1",))])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert '<span class="key sm">1</span>' in html and '<span class="key sm">7</span>' not in html


def test_escaping_in_lines_and_key_caps(dirs):
    trees, out = dirs
    evil = '<img src=x onerror=alert(1)>'
    put(trees, "8885550199", "a", "2026-10-01",
        [obs(text=f"{evil}. Press 1 {evil}, for x press 2 {evil}", keys=("1", "2"), labels={"1": evil, "2": evil})])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_menu_text_loaded_from_sibling_file_not_sys_path(monkeypatch):
    import types
    decoy = types.ModuleType("menu_text")
    decoy.menu_lines = lambda t, k=(): []
    monkeypatch.setitem(sys.modules, "menu_text", decoy)
    monkeypatch.setitem(sys.modules, "zeroout.menu_text", decoy)
    fn = site_build.load_menu_lines()
    assert fn is not decoy.menu_lines
    assert Path(fn.__code__.co_filename) == Path(site_build.__file__).with_name("menu_text.py")


def test_missing_menu_text_refuses(dirs, capsys, monkeypatch):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [obs()])
    monkeypatch.setattr(site_build, "__file__", str(out.parent / "site_build.py"))
    assert site_build.load_menu_lines() is None
    with pytest.raises(FileNotFoundError):
        site_build.build(trees, out, today=TODAY)
    monkeypatch.setattr(site_build, "load_check", lambda: (lambda rel, body: []))
    assert site_build.main([str(trees), str(out)]) == 2
    assert "menu_text" in capsys.readouterr().out


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
    assert (out / "site.js").is_file()
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

    assert snap(out) == snap(out2) and len(snap(out)) == 4


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
    assert "No menus yet" in (out / "index.html").read_text(encoding="utf-8")


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


def test_unicode_lookalike_key_words_do_not_break_the_build(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01",
        [obs(text="Press \u017fix for help. Press \u017ftar. Please select one of the following.", keys=("1",))])
    r = site_build.build(trees, out, today=TODAY)
    assert r["companies"] == 1
    html = page(out, "8885550199")
    assert "Press \u017fix for help." in html and '<span class="key sm">1</span>' in html
    assert all("key" not in q for q in re.findall(r'<ol class="lines">.*?</ol>', html))


def test_a_replay_option_keeps_its_row_and_says_it_replays_a_menu(dirs):
    trees, out = dirs
    put(trees, "8885550199", "a", "2026-10-01", [
        obs(text="For billing press 1. To repeat this information press star.", keys=("1", "*"),
            labels={"1": "Billing", "*": "To repeat this information"})])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    assert html.count("Replays a menu") == 1
    assert ('<li><span class="key sm">*</span><span class="t">To repeat this information</span>'
            ' <span class="state">Replays a menu</span></li>') in html
    assert '<li><span class="key sm">1</span><span class="t">Billing</span></li>' in html


def test_bare_labels_in_community_files_are_filled_from_the_prompt(dirs):
    trees, out = dirs
    menu = ("Thank you for calling Costco. For billing questions, press 1. For Costco Travel, press 5. "
            "To repeat this information, press star.")
    put(trees, "8885550199", "a", "2026-10-01", [
        obs(text=menu, keys=("1", "5", "7", "*"), labels={"1": "one", "5": "", "7": "press 7", "*": "star"}),
        obs("5", text="Travel desk. Press 1 for cruises.", keys=("1",), labels={"1": "1"}),
    ])
    site_build.build(trees, out, today=TODAY)
    html = page(out, "8885550199")
    for k, label in (("1", "billing questions"), ("5", "Costco Travel"), ("7", "Option 7")):
        assert f'<li><span class="key sm">{k}</span><span class="t">{label}</span></li>' in html
    assert ('<li><span class="key sm">*</span><span class="t">repeat this information</span>'
            ' <span class="state">Replays a menu</span></li>') in html
    assert '<span class="t">cruises</span>' in html
    assert '</span> Costco Travel <span class="state">' in html  # the node's own heading, from its parent's prompt
