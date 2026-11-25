from verilays.platforms import describe


def test_mismatch_names_each_differing_field_with_expected_and_got():
    text = describe("mismatch", {"key": {"id": 1}, "diff": {"room": {"expected": 4, "got": "9"}}})

    assert text.startswith("Mismatch")
    assert "id=1" in text
    assert "room: expected 4, got 9" in text


def test_a_custom_checks_evidence_is_shown_not_dropped():
    text = describe("verified", {"source": "crm", "key": "order-42"})

    assert "source: crm" in text
    assert "key: order-42" in text
