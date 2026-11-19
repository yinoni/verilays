import datetime

import pytest

import verilays
from tests.conftest import DSN


def never_checked(params, result, ctx):
    raise AssertionError("the wrapper must not run checks")


def test_init_creates_table_with_every_spec_column_and_is_idempotent(db):
    verilays.init(DSN)  # second call must not fail
    columns = {
        r["column_name"]: r["is_nullable"]
        for r in db.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'verilays_actions'"
        )
    }
    assert set(columns) == {
        "action_id", "run_id", "action_type", "system", "entity_key", "params", "result",
        "error", "status", "evidence", "duplicate_of", "attempts", "next_check_at",
        "deadline_at", "created_at", "updated_at",
    }
    assert columns["run_id"] == columns["duplicate_of"] == "YES"


def test_wrapped_tool_returns_result_unchanged_and_records_one_pending_row(db):
    returned = {"id": 42, "start": datetime.time(10, 0)}

    @verilays.action(type="create_booking", system="postgres:bookings", verify=never_checked)
    def create_booking(book_id, start_time, room="A"):
        return returned

    assert create_booking(42, start_time="10:00") is returned

    rows = db.execute("SELECT * FROM verilays_actions").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["action_type"] == "create_booking"
    assert row["system"] == "postgres:bookings"
    assert row["params"] == {"book_id": 42, "start_time": "10:00", "room": "A"}
    assert row["result"] == {"id": 42, "start": "10:00:00"}  # non-JSON value stored as str
    assert row["attempts"] == 0


def test_each_call_inserts_its_own_row(db):
    @verilays.action(type="ping", system="test", verify=never_checked)
    def ping():
        return None

    ping()
    ping()
    assert db.execute("SELECT count(*) AS n FROM verilays_actions").fetchone()["n"] == 2


def test_verify_is_required():
    with pytest.raises(TypeError, match="verify"):
        verilays.action(type="create_booking", system="postgres:bookings")


def test_verify_that_is_not_a_function_fails_at_import():
    with pytest.raises(TypeError, match="verify must be a function"):
        verilays.action(type="create_booking", system="postgres:bookings", verify="oops")


def test_verify_that_cannot_take_params_result_row_fails_at_import():
    with pytest.raises(TypeError):
        verilays.action(type="create_booking", system="postgres:bookings",
                        verify=lambda params, result: None)
