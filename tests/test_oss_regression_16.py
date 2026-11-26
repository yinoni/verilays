import pytest

import verilays
from verilays.checks import CheckResult


def test_custom_finder_projects_the_action_and_checks_the_record(monkeypatch):
    custom = getattr(verilays.action, "custom", None)
    assert callable(custom), "action.custom must accept a finder"
    monkeypatch.setattr(verilays, "_dsn", "unused")
    monkeypatch.setattr(verilays, "registry", {})
    saved = []
    monkeypatch.setattr(verilays.store, "insert_action", lambda *args, **kwargs: saved.append(args))

    records = {
        11: {"email": "alice@example.com", "score": 4, "internal": "extra"},
        12: {"email": "alice@example.com", "score": "4"},
        13: [],
    }

    @custom("create_contact", find=records.get, lookup_field={"id": "contact_id"},
            fields_to_compare={"email": "address", "score": "score"})
    def create_contact(contact_id, address, score, private):
        return {"contact_id": contact_id + 1, "private": private}

    assert create_contact(10, "alice@example.com", 4, "extra") == {
        "contact_id": 11, "private": "extra",
    }
    [(dsn, action_type, system, params, result, deadline)] = saved
    assert params == {"contact_id": 10, "address": "alice@example.com", "score": 4}
    assert result == {"contact_id": 11}

    verify = verilays.registry["create_contact"]["verify"]
    assert verify(params, result, {}) == CheckResult.verified(email="alice@example.com", score=4)
    assert verify(params, {"contact_id": 12}, {}) == CheckResult.mismatch(
        diff={"score": {"expected": 4, "got": "4"}},
    )
    assert verify(params, None, {}) == CheckResult.not_found()
    with pytest.raises(TypeError, match="find must return a dict or None"):
        verify(params, {"contact_id": 13}, {})
