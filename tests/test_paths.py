import pytest

from zeroout.db import add_company, connect
from zeroout.models import Step
from zeroout.paths import build_path, is_toll_free, normalize_number, press_times


@pytest.fixture
def db():
    conn = connect(":memory:")
    cid = add_company(conn, "Acme", "+18005551234")
    conn.executemany(
        "INSERT INTO nodes (company_id, path, prompt_seconds, timing_correction) VALUES (?, ?, ?, ?)",
        [(cid, "", 12, None), (cid, "2", 8, 0), (cid, "2.1", None, 2)],
    )
    conn.commit()
    return conn, cid


def test_normalize_vanity():
    assert normalize_number("1-800-FLOWERS") == "+18003569377"


def test_normalize_lowercase_vanity():
    assert normalize_number("1-800-flowers") == "+18003569377"


def test_normalize_formats():
    assert normalize_number("(888) 555 1234") == "+18885551234"


def test_normalize_plus_one_punctuation():
    assert normalize_number("+1 (800) 555-1234") == "+18005551234"


@pytest.mark.parametrize("raw", ["12345", "", "   ", "180055512345", "28005551234"])
def test_normalize_rejects_garbage(raw):
    with pytest.raises(ValueError, match="Enter a US number like 1-800-555-1234"):
        normalize_number(raw)


def test_toll_free():
    assert is_toll_free("+18005551234") and not is_toll_free("+12125551234")


@pytest.mark.parametrize("ac", ["800", "888", "877", "866", "855", "844", "833"])
def test_toll_free_all_codes(ac):
    assert is_toll_free(f"+1{ac}5551234")


def test_toll_free_rejects_non_us_and_other():
    assert not is_toll_free("+448005551234")
    assert not is_toll_free("+18225551234")
    assert not is_toll_free("")


def test_build_path_uses_parent_prompt_margin_and_correction(db):
    conn, cid = db
    assert build_path(conn, cid, "2.1") == [Step("2", 13.5), Step("1", 11.5)]


def test_build_path_root_is_empty(db):
    conn, cid = db
    assert build_path(conn, cid, "") == []


def test_build_path_missing_nodes_count_as_zero(db):
    conn, cid = db
    assert build_path(conn, cid, "9.9") == [Step("9", 13.5), Step("9", 1.5)]


def test_build_path_null_prompt_seconds(db):
    conn, cid = db
    assert build_path(conn, cid, "2.1.5") == [Step("2", 13.5), Step("1", 11.5), Step("5", 1.5)]


def test_build_path_custom_margin(db):
    conn, cid = db
    assert build_path(conn, cid, "2", margin=0.5) == [Step("2", 12.5)]


def test_build_path_star_and_hash_keys(db):
    conn, cid = db
    assert [s.key for s in build_path(conn, cid, "*.#")] == ["*", "#"]


def test_press_times():
    assert press_times([Step("2", 13.5), Step("1", 11.5)]) == [13.5, 25.0]


def test_press_times_empty():
    assert press_times([]) == []


def test_effective_wait_rounds_up_to_half_second():
    from zeroout.paths import effective_wait

    assert [effective_wait(w) for w in (0, 0.5, 1.0, 7.7, 7.01, 13.5, 8.0)] == [0, 0.5, 1.0, 8.0, 7.5, 13.5, 8.0]


def test_press_times_use_effective_wait():
    assert press_times([Step("1", 7.7), Step("2", 3.2)]) == [8.0, 11.5]
