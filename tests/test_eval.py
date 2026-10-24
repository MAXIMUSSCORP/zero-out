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
