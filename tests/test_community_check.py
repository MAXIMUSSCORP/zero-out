import copy
import json

import pytest

from zeroout import community_check as cc

REL = "trees/8885550199/2026-10-25-0123456789abcdef.json"


def obs(**kw):
    d = {"path": "", "prompt_text": "For billing press 1", "options": [{"key": "1", "label": "billing"}],
         "outcome": "menu", "prompt_seconds": 19.04, "heard_at": "2026-10-25", "source": "crawl"}
    d.update(kw)
    return d


def doc(**kw):
    d = {"format": 1, "number": "8885550199", "name": "Acme Wireless",
         "contributed_at": "2026-10-25", "observations": [obs()]}
    d.update(kw)
    return d


def body(d) -> bytes:
    return (json.dumps(d, indent=2, sort_keys=True) + "\n").encode()


def errors(d=None, rel=REL, raw=None):
    return cc.check(rel, body(d if d is not None else doc()) if raw is None else raw)


def test_a_valid_file_has_no_errors():
    assert errors() == []


def test_every_outcome_and_source_is_accepted():
    for outcome in cc.OUTCOMES:
        assert errors(doc(observations=[obs(outcome=outcome, options=[])])) == []
    assert errors(doc(observations=[obs(source="phone")])) == []


@pytest.mark.parametrize("rel", [
    "trees/8885550199/x.json",
    "trees/888555019/2026-10-25-0123456789abcdef.json",
    "other/8885550199/2026-10-25-0123456789abcdef.json",
    "trees/8885550199/2026-10-25-0123456789ABCDEF.json",
    "trees/8885550199/2026-10-25-0123456789abcde.json",
    "trees/8885550199/2026-10-25-0123456789abcdef0.json",
    "trees/8885550199/0123456789ab-20261025T183000Z.json",
    "trees/8885550199/2026-10-25-0123456789abcdef.json.txt",
    "../trees/8885550199/2026-10-25-0123456789abcdef.json",
    "trees/\uff18\uff18\uff18\uff15\uff15\uff15\uff10\uff11\uff19\uff19/2026-10-25-0123456789abcdef.json",
    "",
])
def test_bad_paths(rel):
    assert any("The path" in e for e in errors(rel=rel))


def test_folder_must_equal_number():
    out = errors(rel="trees/8885550100/2026-10-25-0123456789abcdef.json")
    assert any("8885550100" in e and "8885550199" in e for e in out)


@pytest.mark.parametrize("bad", [2, 0, True, "1", None])
def test_format_must_be_one(bad):
    assert any("format must be 1" in e for e in errors(doc(format=bad)))


@pytest.mark.parametrize("number", ["4165550199", "8005550199", "888555019", "88855501999", "+8885550199", 8885550199, None])
def test_number_must_be_us_toll_free(number):
    rel = f"trees/{number}/2026-10-25-0123456789abcdef.json" if isinstance(number, str) else REL
    d = doc(number=number)
    if number == "8005550199":
        assert errors(d, rel=rel) == []  # 800 is toll free
        return
    assert any("number must be" in e for e in errors(d, rel=rel))


@pytest.mark.parametrize("area", ["800", "888", "877", "866", "855", "844", "833"])
def test_each_toll_free_area_code(area):
    n = area + "5550199"
    assert errors(doc(number=n), rel=f"trees/{n}/2026-10-25-0123456789abcdef.json") == []


def test_non_toll_free_800_lookalike():
    n = "8095550199"
    assert errors(doc(number=n), rel=f"trees/{n}/2026-10-25-0123456789abcdef.json")


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "Ac\nme", "Ac\x00me", None, 5])
def test_bad_names(name):
    assert errors(doc(name=name))


def test_name_of_80_is_fine():
    assert errors(doc(name="x" * 80)) == []


def test_zero_and_too_many_observations():
    assert any("1 to 500" in e for e in errors(doc(observations=[])))
    assert any("1 to 500" in e for e in errors(doc(observations=[obs()] * 501)))
    assert errors(doc(observations=[obs()] * 500)) == []
    assert any("1 to 500" in e for e in errors(doc(observations="x")))


@pytest.mark.parametrize("path", ["a", "1..2", ".1", "1.", "12", "1.2.", "1,2", " 1", "1.2.3.4.5.6.7", None, 1])
def test_bad_observation_paths(path):
    assert any(".path" in e for e in errors(doc(observations=[obs(path=path)])))


@pytest.mark.parametrize("path", ["", "1", "*", "#", "0.9.*.#", "1.2.3.4.5.6"])
def test_good_observation_paths(path):
    assert errors(doc(observations=[obs(path=path)])) == []


def test_unknown_outcome():
    out = errors(doc(observations=[obs(outcome="robot")]))
    assert any("outcome" in e and "robot" in e for e in out)


@pytest.mark.parametrize("opts", [
    [{"key": "a", "label": "x"}],
    [{"key": "10", "label": "x"}],
    [{"key": "1", "label": "x"}, {"key": "1", "label": "y"}],
    [{"key": "1", "label": "x" * 121}],
    [{"key": "1", "label": 5}],
    [{"key": 1, "label": "x"}],
    [{"key": "1"}],
    [{"key": "1", "label": "x", "extra": 1}],
    ["1"],
    "1",
])
def test_bad_options(opts):
    assert errors(doc(observations=[obs(options=opts)]))


def test_label_of_120_is_fine_and_text_limit():
    assert errors(doc(observations=[obs(options=[{"key": "1", "label": "x" * 120}])])) == []
    assert any("prompt_text" in e for e in errors(doc(observations=[obs(prompt_text="x" * 1001)])))
    assert errors(doc(observations=[obs(prompt_text="x" * 1000)])) == []


@pytest.mark.parametrize("secs", [-0.1, 120.1, "5", None, True, [1]])
def test_bad_prompt_seconds(secs):
    assert any("prompt_seconds" in e for e in errors(doc(observations=[obs(prompt_seconds=secs)])))


def test_prompt_seconds_boundaries():
    for secs in (0, 0.0, 120, 120.0, 3):
        assert errors(doc(observations=[obs(prompt_seconds=secs)])) == []


@pytest.mark.parametrize("stamp", ["2026-10-25T18:01:02Z", "2026-10-25T18:01:02", "20261025", "2026-13-25",
                                    "2026-02-30", "2026-10-25 ", "\uff12\uff10\uff12\uff16-10-25", None, 5])
def test_bad_dates(stamp):
    assert any("heard_at" in e for e in errors(doc(observations=[obs(heard_at=stamp)])))
    assert any("contributed_at" in e for e in errors(doc(contributed_at=stamp)))


def test_contributed_at_must_equal_the_file_name_date():
    out = errors(doc(contributed_at="2026-10-26"))
    assert any("2026-10-25" in e and "2026-10-26" in e for e in out)


def test_heard_at_may_not_be_after_contributed_at():
    assert any("is after contributed_at" in e for e in errors(doc(observations=[obs(heard_at="2026-10-26")])))
    assert errors(doc(observations=[obs(heard_at="2026-10-24"), obs(heard_at="2026-10-25")])) == []


def test_bad_source():
    assert any("source" in e for e in errors(doc(observations=[obs(source="community")])))


@pytest.mark.parametrize("text", [
    "account 1234", "call 780 555 0199", "call 780-555-0199", "call 780.555.0199", "1 2 3 4",
    "4, 5, 6, 7", "(415) 555-1234", "415, 555, 1234", "415/555/1234", "one two three four",
    "oh one two three", "uno dos tres cuatro", "Say ONE Two three FOUR",
])
def test_digit_runs_redact_would_catch_are_refused_in_text_and_labels(text):
    assert any("prompt_text" in e and "digits" in e for e in errors(doc(observations=[obs(prompt_text=text)])))
    assert any("label" in e and "digits" in e for e in errors(doc(observations=[obs(options=[{"key": "1", "label": text}])])))


def test_short_digit_runs_are_fine():
    assert errors(doc(observations=[obs(prompt_text="open 24 hours, press 1 2 3 and [number] then 123")])) == []
    assert errors(doc(observations=[obs(prompt_text="one two three, then four")])) == []


def test_fullwidth_digits_do_not_pass_as_digits_or_as_a_number():
    # a fullwidth number is not matched by an ASCII-only pattern, so it must fail on its own terms
    n = "\uff18\uff18\uff18\uff15\uff15\uff15\uff10\uff11\uff19\uff19"
    assert errors(doc(number=n))
    assert errors(doc(contributed_at="\uff12\uff10\uff12\uff16-10-25"))


def test_name_with_a_digit_run_is_refused():
    assert any("name" in e and "digits" in e for e in errors(doc(name="Visa card 4111 1111 1111 1111")))
    assert errors(doc(name="Acme 24 Hour Wireless")) == []


def test_digit_pattern_is_the_same_as_redacts():
    from zeroout import redact
    assert cc.DIGITS.pattern == redact.DIGIT_RUN.pattern and cc.DIGITS.flags == redact.DIGIT_RUN.flags


@pytest.mark.parametrize("char", ["\u200b", "\u202e", "\u2066", "\ufeff", "\u200d"])
@pytest.mark.parametrize("where", ["name", "prompt_text", "label"])
def test_invisible_formatting_characters_are_refused(where, char):
    bad = f"a{char}b"
    if where == "name":
        d = doc(name=bad)
    elif where == "prompt_text":
        d = doc(observations=[obs(prompt_text=bad)])
    else:
        d = doc(observations=[obs(options=[{"key": "1", "label": bad}])])
    assert any("hidden formatting character" in e for e in errors(d))


@pytest.mark.parametrize("where", ["prompt_text", "label", "name"])
def test_control_characters_are_refused_anywhere(where):
    bad = "a\nb"
    if where == "prompt_text":
        d = doc(observations=[obs(prompt_text=bad)])
    elif where == "label":
        d = doc(observations=[obs(options=[{"key": "1", "label": "a\tb"}])])
    else:
        d = doc(name=bad)
    assert any("control character" in e for e in errors(d))


def test_control_character_in_a_field_name():
    d = doc()
    d["a\x01b"] = 1
    assert any("control character" in e for e in errors(d))


def test_unknown_keys_at_both_levels():
    assert any("unknown field" in e and "extra" in e for e in errors(doc(extra=1)))
    d = doc()
    d["observations"][0]["extra"] = 1
    assert any("unknown field" in e for e in errors(d))


def test_missing_fields():
    d = doc()
    del d["name"]
    del d["observations"][0]["source"]
    out = errors(d)
    assert any("missing the field name" in e for e in out)
    assert any("observations[0] is missing the field source" in e for e in out)


def test_body_over_256_kb():
    out = cc.check(REL, b" " * (256 * 1024 + 1))
    assert any("limit is 262144" in e for e in out)
    assert cc.check(REL, body(doc()) + b" " * 100) == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"3", b"null", b"\xff\xfe", b"", b'{"format": NaN}', b"[" * 100000])
def test_not_a_json_object(raw):
    assert errors(raw=raw)


def test_nan_prompt_seconds_is_refused():
    raw = body(doc()).replace(b"19.04", b"NaN")
    assert errors(raw=raw)


def test_errors_are_plain_sentences_naming_the_value():
    out = errors(doc(observations=[obs(outcome="robot", source="x")]))
    assert out and all(e.endswith(".") for e in out)
    assert any("'robot'" in e for e in out)


def test_error_list_is_capped():
    out = errors(doc(observations=[obs(outcome="robot")] * 100))
    assert len(out) == cc.MAX_ERRORS + 1 and "more problems" in out[-1]


def test_does_not_mutate_input_and_is_pure():
    d = doc()
    before = copy.deepcopy(d)
    errors(d)
    assert d == before


def test_uid_is_stable_and_ignores_key_order():
    a = obs()
    b = dict(reversed(list(a.items())))
    assert cc.observation_uid("8885550199", a) == cc.observation_uid("8885550199", b)
    assert len(cc.observation_uid("8885550199", a)) == 64


def test_uid_changes_with_number_or_content():
    base = cc.observation_uid("8885550199", obs())
    assert cc.observation_uid("8885550198", obs()) != base
    assert cc.observation_uid("8885550199", obs(prompt_text="x")) != base
    assert cc.observation_uid("8885550199", obs(options=[{"label": "billing", "key": "1"}])) == base


def test_uid_known_value_so_the_repo_ci_copy_cannot_drift():
    import hashlib
    canon = '{"number":"8885550199","observation":{"a":1}}'
    assert cc.observation_uid("8885550199", {"a": 1}) == hashlib.sha256(canon.encode()).hexdigest()


def test_module_imports_only_the_standard_library():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(cc.__file__).read_text())
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"hashlib", "json", "os", "re", "sys", "unicodedata", "datetime"}


# ---------- command line, as the trees repo's CI runs it ----------

def run_cli(folder, cwd):
    import subprocess
    import sys
    return subprocess.run([sys.executable, "-I", cc.__file__, str(folder)], cwd=cwd,
                          capture_output=True, text=True)


def put(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else body(data))


def test_cli_passes_a_valid_tree(tmp_path):
    trees = tmp_path / "trees"
    put(trees, "8885550199/2026-10-25-0123456789abcdef.json", doc())
    put(trees, ".gitkeep", b"")
    r = run_cli(trees, tmp_path)
    assert r.returncode == 0 and r.stdout == "  checked  1 files, 0 invalid\n"


def test_cli_empty_folder_is_fine(tmp_path):
    (tmp_path / "trees").mkdir()
    r = run_cli(tmp_path / "trees", tmp_path)
    assert r.returncode == 0 and "checked  0 files, 0 invalid" in r.stdout


def test_cli_reports_invalid_files_and_exits_1(tmp_path):
    trees = tmp_path / "trees"
    put(trees, "8885550199/2026-10-25-0123456789abcdef.json", doc())
    put(trees, "8885550199/2026-10-25-aaaaaaaaaaaaaaaa.json", doc(observations=[obs(outcome="robot")]))
    r = run_cli(trees, tmp_path)
    lines = r.stdout.splitlines()
    assert r.returncode == 1
    assert lines[0].startswith("  invalid  trees/8885550199/2026-10-25-aaaaaaaaaaaaaaaa.json: ")
    assert "robot" in lines[0] and lines[-1] == "  checked  2 files, 1 invalid"


def test_cli_refuses_stray_files_and_wrong_layout(tmp_path):
    trees = tmp_path / "trees"
    put(trees, "README.md", b"hello")
    put(trees, "notes.json", doc())
    put(trees, "8885550100/2026-10-25-0123456789abcdef.json", doc())  # folder differs from number
    r = run_cli(trees, tmp_path)
    assert r.returncode == 1 and r.stdout.count("  invalid  ") == 3
    assert "trees/README.md" in r.stdout and r.stdout.splitlines()[-1] == "  checked  3 files, 3 invalid"


def test_cli_paths_are_relative_to_the_folder_given(tmp_path):
    elsewhere = tmp_path / "checkout" / "data"
    put(elsewhere, "8885550199/2026-10-25-0123456789abcdef.json", doc())
    assert run_cli(elsewhere, tmp_path).returncode == 0


def test_cli_missing_folder_and_bad_usage(tmp_path):
    assert run_cli(tmp_path / "nope", tmp_path).returncode == 1
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-I", cc.__file__], capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr


def test_cli_works_with_no_zeroout_on_the_path(tmp_path):
    import shutil
    lone = tmp_path / "tools"
    lone.mkdir()
    shutil.copy(cc.__file__, lone / "community_check.py")
    trees = tmp_path / "trees"
    put(trees, "8885550199/2026-10-25-0123456789abcdef.json", doc())
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-I", str(lone / "community_check.py"), "trees"], cwd=tmp_path,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cli_refuses_a_file_dated_beyond_tomorrow(tmp_path):
    trees = tmp_path / "trees"
    put(trees, "8885550199/2099-01-01-0123456789abcdef.json",
        doc(contributed_at="2099-01-01", observations=[obs(heard_at="2099-01-01")]))
    r = run_cli(trees, tmp_path)
    assert r.returncode == 1 and "in the future" in r.stdout
    # check() itself stays pure: it has no clock
    assert cc.check("trees/8885550199/2099-01-01-0123456789abcdef.json",
                    body(doc(contributed_at="2099-01-01", observations=[obs(heard_at="2099-01-01")]))) == []


def test_cli_accepts_a_file_dated_tomorrow(tmp_path):
    from datetime import datetime, timedelta, timezone
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    trees = tmp_path / "trees"
    put(trees, f"8885550199/{tomorrow}-0123456789abcdef.json", doc(contributed_at=tomorrow, observations=[obs(heard_at=tomorrow)]))
    assert run_cli(trees, tmp_path).returncode == 0


def test_cli_fails_on_a_folder_named_like_a_file(tmp_path):
    trees = tmp_path / "trees"
    (trees / "8885550199" / "2026-10-25-0123456789abcdef.json").mkdir(parents=True)
    r = run_cli(trees, tmp_path)
    assert r.returncode == 1 and "folder" in r.stdout and "checked  1 files, 1 invalid" in r.stdout


@pytest.mark.parametrize("space", [" ", " ", " ", " ", "　"])
def test_unicode_space_digit_runs_are_refused(space):
    text = f"call 415{space}555{space}1234"
    assert any("prompt_text" in e and "digits" in e for e in errors(doc(observations=[obs(prompt_text=text)])))
    assert any("name" in e and "digits" in e for e in errors(doc(name=text)))
