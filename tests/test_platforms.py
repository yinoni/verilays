from verilays.platforms import describe


def test_mismatch_reads_as_the_lookup_then_each_field():
    evidence = {"key": {"id": 1}, "diff": {"room": {"expected": 4, "got": "9"}, "start_time": {"expected": "10:00", "got": "11:00"}}}

    assert describe(evidence) == "Looked up id=1\nroom: expected 4, got 9\nstart_time: expected 10:00, got 11:00"


def test_each_recipe_flag_has_its_own_sentence():
    assert describe({"key": {"id": 2}, "still_exists": True}) == (
        "Looked up id=2\nThe row still exists, but the tool should have deleted it")
    assert describe({"key": {"id": 1}, "changed_after_verify": True}) == (
        "Looked up id=1\nIt matched at first, then changed after it was verified")
    assert describe({"key": {"id": 1}, "rows": ">1"}) == "Looked up id=1\nMore than one row matches"


def test_a_custom_checks_evidence_is_shown_as_key_value():
    assert describe({"source": "crm", "key": "order-42"}) == "source: crm\nkey: order-42"
