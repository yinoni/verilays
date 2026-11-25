from verilays.platforms import describe


def test_mismatch_reads_as_the_status_then_the_lookup_then_each_field():
    evidence = {"key": {"id": 1}, "diff": {"room": {"expected": 4, "got": "9"}, "start_time": {"expected": "10:00", "got": "11:00"}}}

    assert describe("mismatch", evidence) == (
        "Mismatch: the record in the system doesn't match what the tool was expected to do\n"
        "Looked up id=1\nroom: expected 4, got 9\nstart_time: expected 10:00, got 11:00")


def test_each_status_starts_with_its_own_sentence():
    assert describe("verified", {"key": {"id": 1}}) == (
        "Verified: the record is in the system, as the tool reported\nLooked up id=1")
    assert describe("missing", {"key": {"id": 999}}) == (
        "Missing: we didn't find the record in the system before the deadline\nLooked up id=999")
    assert describe("expired", None) == "Expired: the check kept failing until the deadline, so we couldn't tell"


def test_ambiguous_says_it_changed_once_not_twice():
    assert describe("ambiguous", {"key": {"id": 1}, "changed_after_verify": True}) == (
        "Ambiguous: the record matched at first, then changed after it was verified\nLooked up id=1")


def test_row_kept_after_a_delete_explains_the_mismatch():
    assert describe("mismatch", {"key": {"id": 2}, "still_exists": True}).splitlines()[1:] == [
        "Looked up id=2", "The row still exists, but the tool should have deleted it"]


def test_a_custom_checks_evidence_is_shown_as_key_value():
    assert describe("verified", {"source": "crm", "key": "order-42"}).splitlines()[1:] == [
        "source: crm", "key: order-42"]
