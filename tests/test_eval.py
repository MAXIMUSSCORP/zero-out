import pytest
from eval.listen_eval import label_match, score
from eval.report import rows
from zeroout import db, tree
from zeroout.models import Observation, Option


def test_label_match_ignores_filler_and_case():
    assert label_match("Billing", "for billing questions")
    assert not label_match("billing", "sales")


def test_score_counts_keys_and_outcome():
    gold = {"options": [{"key": "1", "label": "billing"}, {"key": "2", "label": "agent"}], "outcome": "menu"}
    pred = Observation("x", [Option("1", "Billing"), Option("3", "agent")], "menu", 2.0)
    assert score(gold, pred) == (1, 2, True)
    assert score(gold, None) == (0, 2, False)


def test_report_rows():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550001")
    rid = db.start_run(conn, cid, "crawl", "crawler")
    conn.execute("UPDATE runs SET cost_usd = 0.5, minutes = 3 WHERE id = ?", (rid,))
    menu = Observation("Press 1", [Option("1", "billing")], "menu", 2.0)
    tree.merge(conn, cid, "", menu, rid, "a.wav", lambda a, b: True)
    tree.merge(conn, cid, "", menu, rid, "b.wav", lambda a, b: True)
    tree.merge(conn, cid, "1", Observation("hold", [], "hold", 1.0), rid, "c.wav", lambda a, b: True)
    assert rows(conn) == [
        {"name": "Acme", "mapped": 2, "cost_usd": 0.5, "minutes": 3.0, "confirmation_rate": 0.5}
    ]


# ---------- listen_eval: flags, metering, json ----------

import json
from types import SimpleNamespace

from eval import compare, time_saved
from eval.listen_eval import false_options, main as eval_main, resolve
from tests.fakes import _wav
from zeroout.config import Settings
from zeroout.models import Word


def test_false_options_counts_predicted_keys_the_gold_lacks():
    gold = {"options": [{"key": "1", "label": "billing"}], "outcome": "menu"}
    pred = Observation("x", [Option("1", "billing"), Option("2", "agent"), Option("3", "x")], "menu", 2.0)
    assert false_options(gold, pred) == 2
    assert false_options(gold, None) == 0
    assert score(gold, pred) == (1, 1, True)


def test_resolve_joins_relative_files_to_the_root_only():
    assert resolve("2/5.wav", "/data") == "/data/2/5.wav"
    assert resolve("/abs/5.wav", "/data") == "/abs/5.wav"
    assert resolve("2/5.wav", None) == "2/5.wav"


class FakeClient:
    """Answers every chat call with one menu and a fixed usage, and remembers the model asked for."""

    def __init__(self):
        self.models = []
        body = {"prompt_text": "Press one for billing.", "options": [{"key": "1", "label": "billing"}],
                "outcome": "menu", "prompt_seconds": 2.0}
        self.resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.models.append(kw["model"])
        return self.resp


def hear(wav, model_dir):
    return [Word("Press", 0.0, 0.5), Word("one", 0.5, 1.0), Word("for", 1.0, 1.3), Word("billing.", 1.3, 2.0)]


def labelled(tmp_path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "a.wav").write_bytes(_wav([0] * 16000))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([{"file": "a.wav", "options": [{"key": "1", "label": "billing"}], "outcome": "menu"}]))
    return labels


def test_eval_main_writes_json_with_model_tokens_and_cost(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("eval.listen_eval.load_settings",
                        lambda: Settings("k", "http://x", "nano-1", "", "", "", "", "", 1.0, ""))
    client, out = FakeClient(), tmp_path / "out.json"
    eval_main([str(labelled(tmp_path)), "--audio-root", str(tmp_path / "audio"), "--model", "big-2",
               "--price", "1.0,4.0", "--json", str(out)], client=client, transcribe=hear)
    assert client.models == ["big-2"]
    printed = capsys.readouterr().out
    assert "key_accuracy=1.000 (1/1)" in printed and "outcome_accuracy=1.000 (1/1)" in printed
    got = json.loads(out.read_text())
    s = got["summary"]
    assert (s["model"], s["clips"], s["key_accuracy"], s["outcome_accuracy"], s["false_options"]) == ("big-2", 1, 1.0, 1.0, 0)
    assert (s["prompt_tokens"], s["completion_tokens"]) == (100, 20)
    assert s["cost_usd"] == pytest.approx((100 * 1.0 + 20 * 4.0) / 1e6)
    c = got["clips"][0]
    assert c["file"] == "a.wav" and c["llm_seconds"] >= 0 and c["asr_seconds"] >= 0 and c["seconds"] >= c["llm_seconds"]


def test_eval_without_price_records_tokens_and_no_cost(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.listen_eval.load_settings",
                        lambda: Settings("k", "http://x", "nano-1", "", "", "", "", "", 1.0, ""))
    out = tmp_path / "out.json"
    eval_main([str(labelled(tmp_path)), "--audio-root", str(tmp_path / "audio"), "--json", str(out)],
              client=FakeClient(), transcribe=hear)
    s = json.loads(out.read_text())["summary"]
    assert s["model"] == "nano-1" and s["cost_usd"] is None and s["prompt_tokens"] == 100


# ---------- compare ----------

def test_compare_prints_one_aligned_row_per_run(tmp_path, capsys):
    for name, model, cost in [("a", "nano-1", None), ("b", "big-2", 0.002)]:
        (tmp_path / f"{name}.json").write_text(json.dumps({"summary": {
            "model": model, "clips": 2, "key_accuracy": 0.5, "outcome_accuracy": 1.0, "false_options": 3,
            "median_seconds": 1.25, "median_llm_seconds": 0.75, "cost_usd": cost, "prompt_tokens": 200, "completion_tokens": 40}}))
    compare.main([str(tmp_path / "a.json"), str(tmp_path / "b.json")])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and "model" in lines[0]
    assert "nano-1" in lines[1] and "50%" in lines[1] and "1.25" in lines[1]
    assert "0.0010" in lines[2]  # cost per menu: 0.002 over 2 clips
    assert len({len(x) for x in lines}) == 1


# ---------- time_saved ----------

def timed_tree(conn, number="+18005550000"):
    cid = db.add_company(conn, "Acme", number)
    rid = db.start_run(conn, cid, "crawl", "crawler")
    menu = lambda txt, opts, secs: Observation(txt, [Option(k, l) for k, l in opts], "menu", secs)
    tree.merge(conn, cid, "", menu("m", [("1", "a")], 4.0), rid, "a.wav", lambda a, b: True)
    tree.merge(conn, cid, "1", menu("m", [("2", "b")], 6.2), rid, "b.wav", lambda a, b: True)
    tree.merge(conn, cid, "1.2", Observation("hold", [], "hold", 1.0), rid, "c.wav", lambda a, b: True)
    return cid


def test_time_saved_rows_use_the_apps_steps_and_recorded_prompts():
    conn = db.connect(":memory:")
    timed_tree(conn)
    got = {r["path"]: r for r in time_saved.rows(conn, margin=1.5)}
    assert sorted(got) == ["1", "1.2"]  # the root has no key to press
    # "1": wait 4.0 + 1.5 = 5.5, then LEAD_SECONDS 2. Manual listens to the root prompt only.
    assert (got["1"]["connect_seconds"], got["1"]["manual_seconds"]) == (7.5, 4.0)
    # "1.2": 5.5 + ceil-to-half(6.2 + 1.5 = 7.7 -> 8.0) = 13.5, + 2. Manual: 4.0 + 6.2.
    assert (got["1.2"]["connect_seconds"], got["1.2"]["manual_seconds"]) == (15.5, 10.2)


def test_time_saved_skips_companies_connect_cannot_dial_and_unmapped_nodes():
    conn = db.connect(":memory:")
    timed_tree(conn, "+14155550123")  # not toll free
    assert time_saved.rows(conn) == []
    conn = db.connect(":memory:")
    cid = timed_tree(conn)
    conn.execute("INSERT INTO nodes (company_id, path, status) VALUES (?, '3', 'unexplored')", (cid,))
    assert {r["path"] for r in time_saved.rows(conn)} == {"1", "1.2"}


def test_report_and_time_saved_read_the_db_named_by_flag(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "z.db")
    conn = db.connect(path)
    timed_tree(conn)
    conn.commit()
    monkeypatch.setattr("eval.report.load_settings", lambda: pytest.fail("settings were read"))
    from eval import report
    report.main(["--db", path])
    assert "Acme" in capsys.readouterr().out
    monkeypatch.setattr("eval.time_saved.load_settings", lambda: SimpleNamespace(db_path="", key_margin=1.5))
    time_saved.main(["--db", path, "--csv", str(tmp_path / "t.csv")])
    out = capsys.readouterr().out
    assert "median" in out and "max" in out
    assert (tmp_path / "t.csv").read_text().splitlines()[0] == "company,path,connect_seconds,manual_seconds,extra_seconds"


# ---------- review fixes ----------

def conf(monkeypatch):
    monkeypatch.setattr("eval.listen_eval.load_settings",
                        lambda: Settings("k", "http://x", "nano-1", "", "", "", "", "", 1.0, ""))


def test_time_saved_keeps_negative_extra_for_a_capped_community_prompt():
    conn = db.connect(":memory:")
    cid = db.add_company(conn, "Acme", "+18005550000")
    conn.execute("INSERT INTO nodes (company_id, path, prompt_seconds, status, source) VALUES (?, '', 35, 'mapped', 'community')", (cid,))
    conn.execute("INSERT INTO nodes (company_id, path, prompt_seconds, status) VALUES (?, '1', 1, 'mapped')", (cid,))
    (r,) = time_saved.rows(conn, margin=1.5)
    assert (r["connect_seconds"], r["manual_seconds"], r["extra_seconds"]) == (23.5, 35, -11.5)


def test_eval_warms_the_speech_model_once_before_the_first_clip(tmp_path, monkeypatch):
    conf(monkeypatch)
    seen = []
    monkeypatch.setattr("zeroout.hear.load", lambda d: seen.append(("load", d)))
    monkeypatch.setattr("zeroout.hear.transcribe", lambda w, d: seen.append(("clip", d)) or hear(w, d))
    eval_main([str(labelled(tmp_path)), "--audio-root", str(tmp_path / "audio")], client=FakeClient())
    assert [s[0] for s in seen] == ["load", "clip"]


def test_eval_marks_tokens_and_cost_unknown_when_usage_is_missing(tmp_path, monkeypatch):
    conf(monkeypatch)
    client, out = FakeClient(), tmp_path / "o.json"
    client.resp.usage = None
    eval_main([str(labelled(tmp_path)), "--audio-root", str(tmp_path / "audio"), "--price", "1,4", "--json", str(out)],
              client=client, transcribe=hear)
    got = json.loads(out.read_text())
    assert got["clips"][0]["prompt_tokens"] is None and got["clips"][0]["cost_usd"] is None
    assert got["summary"]["cost_usd"] is None and got["summary"]["unknown_usage_clips"] == 1


def test_eval_scores_a_silent_clip_the_way_the_crawler_records_it(tmp_path, monkeypatch):
    from zeroout.probe import TAIL_SECONDS
    conf(monkeypatch)
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "s.wav").write_bytes(_wav([0] * (8000 * TAIL_SECONDS)))
    labels = tmp_path / "l.json"
    labels.write_text(json.dumps([{"file": "s.wav", "options": [], "outcome": "hold"}]))
    out = tmp_path / "o.json"
    eval_main([str(labels), "--audio-root", str(tmp_path / "audio"), "--json", str(out)],
              client=FakeClient(), transcribe=lambda w, d: [])
    assert json.loads(out.read_text())["clips"][0]["outcome"] == "hold"


def test_eval_json_records_price_and_labels_path(tmp_path, monkeypatch):
    conf(monkeypatch)
    labels, out = labelled(tmp_path), tmp_path / "o.json"
    eval_main([str(labels), "--audio-root", str(tmp_path / "audio"), "--price", "1,4", "--json", str(out)],
              client=FakeClient(), transcribe=hear)
    s = json.loads(out.read_text())["summary"]
    assert s["price_per_million"] == [1.0, 4.0] and s["labels"] == str(labels)


def test_compare_shows_clips_llm_seconds_and_per_clip_cost(tmp_path, capsys):
    (tmp_path / "a.json").write_text(json.dumps({"summary": {
        "model": "m", "clips": 4, "key_accuracy": 1.0, "outcome_accuracy": 1.0, "false_options": 0,
        "median_seconds": 9.0, "median_llm_seconds": 3.5, "cost_usd": 0.004}}))
    compare.main([str(tmp_path / "a.json")])
    head, line = capsys.readouterr().out.splitlines()
    assert "$ per clip" in head and "llm s" in head and "clips" in head and "menu" not in head
    assert line.split() == ["m", "4", "100%", "100%", "0", "9.00", "3.50", "0.0010"]
