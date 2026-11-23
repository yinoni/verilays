import datetime

from click.testing import CliRunner

from tests.conftest import DSN
from verilays import store
from verilays.cli import main
from verilays.store import _jsonb


def seed(db, tool, status, params=None, evidence=None, error=None, duplicate_of=None, seconds_ago=0):
    """One action row as the wrapper and worker would leave it; returns its action_id."""
    store.insert_action(DSN, tool, "postgres", params or {"room": 4, "start_time": "10:00"}, None)
    action_id = db.execute(
        "SELECT action_id FROM verilays_actions ORDER BY created_at DESC LIMIT 1").fetchone()["action_id"]
    db.execute(
        "UPDATE verilays_actions SET status = %s, evidence = %s, error = %s, duplicate_of = %s,"
        " created_at = created_at - %s, deadline_at = deadline_at - %s WHERE action_id = %s",
        (status, _jsonb(evidence) if evidence else None, error, duplicate_of,
         datetime.timedelta(seconds=seconds_ago), datetime.timedelta(seconds=seconds_ago), action_id))
    return action_id


def report(monkeypatch):
    monkeypatch.setenv("VERILAYS_DSN", DSN)
    result = CliRunner().invoke(main, ["report"])
    assert result.exit_code == 0, result.output
    return result.output


def test_count_table_has_one_row_per_tool_and_one_column_per_status(db, monkeypatch):
    seed(db, "create_booking", "verified")
    seed(db, "create_booking", "verified")
    seed(db, "create_booking", "mismatch", evidence={"diff": {}})
    seed(db, "send_email", "pending")

    out = report(monkeypatch)

    assert out.splitlines()[:3] == [
        "                verified  pending  mismatch",
        "create_booking         2        0         1",
        "send_email             0        1         0",
    ]


def test_a_status_the_report_does_not_know_still_gets_a_column(db, monkeypatch):
    seed(db, "create_booking", "verified")
    seed(db, "create_booking", "on_hold")

    out = report(monkeypatch)

    assert out.splitlines()[:2] == ["                verified  on_hold",
                                    "create_booking         1        1"]


def test_needs_attention_lists_problems_with_a_reason_and_skips_good_rows(db, monkeypatch):
    seed(db, "create_booking", "verified", params={"room": 1})
    seed(db, "create_booking", "pending", params={"room": 2})
    seed(db, "create_booking", "mismatch", evidence={"diff": {"start_time": {"expected": "10:00", "got": "11:00:00"}}})
    seed(db, "create_booking", "failed", error="RuntimeError('api down')")
    seed(db, "create_booking", "missing")
    seed(db, "send_email", "expired", error="ConnectionError('crm is down')")
    seed(db, "create_booking", "check_error", error="VERILAYS_POSTGRES_BOOKINGS is not set")
    first = seed(db, "create_booking", "verified", params={"room": 9}, seconds_ago=15)
    seed(db, "create_booking", "verified", params={"room": 9}, duplicate_of=first)

    out = report(monkeypatch)

    attention = out.split("Needs attention\n")[1]
    assert "mismatch     room=4 start_time=10:00  start_time: expected 10:00, got 11:00:00" in attention
    assert "failed       room=4 start_time=10:00  RuntimeError('api down')" in attention
    assert "missing      room=4 start_time=10:00  not found after 0:01:00" in attention
    assert "expired      room=4 start_time=10:00  no check succeeded: ConnectionError('crm is down')" in attention
    assert "check_error  room=4 start_time=10:00  VERILAYS_POSTGRES_BOOKINGS is not set" in attention
    assert f"duplicate    room=9  duplicate of {str(first)[:8]}, 15s later" in attention
    assert "room=1" not in attention and "room=2" not in attention
    assert len(attention.splitlines()) == 6


def test_ambiguous_says_what_changed_after_verified(db, monkeypatch):
    seed(db, "create_booking", "ambiguous", params={"room": 4}, evidence={
        "changed_after_verify": True, "key": {"id": 1},
        "diff": {"start_time": {"expected": "10:00", "got": "11:00:00"}}})
    seed(db, "create_booking", "ambiguous", params={"room": 5},
         evidence={"changed_after_verify": True, "key": {"id": 2}})

    attention = report(monkeypatch).split("Needs attention\n")[1]

    assert "ambiguous    room=4  changed after verified: start_time 10:00 → 11:00:00" in attention
    assert "ambiguous    room=5  changed after verified: no longer found" in attention


def test_empty_store_says_no_actions_recorded(db, monkeypatch):
    assert report(monkeypatch) == "no actions recorded\n"


def test_report_without_a_dsn_fails_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("VERILAYS_DSN", raising=False)

    result = CliRunner().invoke(main, ["report"])

    assert result.exit_code != 0
    assert "set VERILAYS_DSN" in result.output
