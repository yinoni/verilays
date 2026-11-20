import asyncio
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
        "error", "status", "evidence", "check", "duplicate_of", "attempts", "next_check_at",
        "deadline_at", "created_at", "updated_at",
    }
    assert columns["run_id"] == columns["duplicate_of"] == columns["check"] == "YES"


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


def test_tool_that_raises_reraises_the_same_error_and_records_a_failed_row(db):
    @verilays.action(type="create_booking", system="test", verify=never_checked)
    def create_booking(room):
        raise ValueError(f"room {room} taken")

    with pytest.raises(ValueError, match="room 4 taken"):
        create_booking(4)

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "failed"
    assert row["params"] == {"room": 4}
    assert row["error"] == "ValueError('room 4 taken')"


def test_async_tool_returns_its_result_and_records_a_pending_row(db):
    @verilays.action(type="create_booking", system="test", verify=never_checked)
    async def create_booking(room, n=1):
        return {"room": room}

    assert asyncio.run(create_booking(6)) == {"room": 6}

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "pending"
    assert row["params"] == {"room": 6, "n": 1}


def test_async_tool_that_raises_reraises_the_same_error_and_records_a_failed_row(db):
    @verilays.action(type="create_booking", system="test", verify=never_checked)
    async def create_booking(room):
        raise ValueError(f"room {room} taken")

    with pytest.raises(ValueError, match="room 5 taken"):
        asyncio.run(create_booking(5))

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "ValueError('room 5 taken')"


def test_store_down_tool_still_returns_and_a_warning_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(verilays, "_dsn", "postgresql://x@localhost:1/x")  # closed port

    @verilays.action(type="create_booking", system="test", verify=never_checked)
    def create_booking(room):
        return {"room": room}

    @verilays.action(type="create_booking_async", system="test", verify=never_checked)
    async def create_booking_async(room):
        return {"room": room}

    assert create_booking(7) == {"room": 7}
    assert asyncio.run(create_booking_async(8)) == {"room": 8}
    assert caplog.text.count("insert record error") == 2


def test_action_postgres_saves_its_check_in_the_row_and_needs_no_verify(db):
    @verilays.action.postgres("create_booking", db="bookings", table="bookings",
                              lookup_field="id", fields_to_compare=["room", "start_time"])
    def create_booking(room, start_time):
        return {"id": 1}

    assert create_booking(4, "10:00") == {"id": 1}

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["system"] == "postgres"
    assert row["check"] == {"db": "bookings", "table": "bookings", "lookup_field": "id",
                            "fields_to_compare": ["room", "start_time"]}
    assert verilays.registry == {}
