import datetime
import logging

import psycopg
import pytest

import verilays
from tests.conftest import DSN
from verilays import recipes, store
from verilays.checks import CheckResult
from verilays.worker import run_once


def bookings_conn():
    return psycopg.connect(DSN)


@pytest.fixture
def bookings(db):
    """The customer's own table, in the same Postgres."""
    db.execute("DROP TABLE IF EXISTS bookings")
    db.execute("CREATE TABLE bookings (id serial PRIMARY KEY, room int, start_time time)")
    yield db
    db.execute("DROP TABLE bookings")


def book_tool(writes=True, returns_dict=True):
    """A create_booking tool that claims success; with writes=False it never saves."""

    @verilays.action(
        type="create_booking",
        system="postgres:bookings",
        verify=recipes.pg_row_matches(
            connect=bookings_conn,
            table="bookings",
            lookup_field="id",
            fields_to_compare=["room", "start_time"],
        ),
    )
    def create_booking(room, start_time):
        if not writes:
            return {"id": 999}
        with bookings_conn() as conn:
            row = conn.execute(
                "INSERT INTO bookings (room, start_time) VALUES (%s, %s) RETURNING id",
                (room, start_time),
            ).fetchone()
        return {"id": row[0]} if returns_dict else row[0]

    return create_booking


def only_row(db):
    rows = db.execute("SELECT * FROM verilays_actions").fetchall()
    assert len(rows) == 1
    return rows[0]


def test_saved_booking_is_verified_and_the_decision_is_logged(bookings, caplog):
    bookings.execute("INSERT INTO bookings (room, start_time) VALUES (5, '12:00')")  # decoy, id 1
    book_tool()(room=4, start_time="10:00")  # id 2
    now = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once(now=now)

    row = only_row(bookings)
    assert row["status"] == "verified"
    assert row["attempts"] == 1
    assert row["updated_at"] == now
    assert row["evidence"] == {"key": {"id": 2}, "row": {"room": "4", "start_time": "10:00:00"}}
    assert "→ verified (check verified, attempt 1)" in caplog.text
    assert "10:00:00" in caplog.text  # the value behind the decision


def test_booking_never_saved_stays_pending_and_is_retried(bookings, caplog):
    book_tool(writes=False)(room=4, start_time="10:00")

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()
        run_once()

    row = only_row(bookings)
    assert row["status"] == "pending"
    assert row["attempts"] == 2
    assert row["evidence"] == {"key": {"id": 999}}
    assert "→ pending (check not_found, attempt 2)" in caplog.text


def test_wrong_value_stays_pending_with_a_field_diff(bookings):
    book_tool()(room=4, start_time="10:00")
    bookings.execute("UPDATE bookings SET start_time = '11:00'")

    run_once()

    row = only_row(bookings)
    assert row["status"] == "pending"
    assert row["evidence"]["diff"] == {
        "start_time": {"expected": "10:00", "got": "11:00:00"}
    }


def test_two_matching_rows_are_ambiguous_and_stay_pending(bookings, caplog):
    @verilays.action(
        type="book_room",
        system="postgres:bookings",
        verify=recipes.pg_row_matches(bookings_conn, "bookings", lookup_field="room", fields_to_compare=[]),
    )
    def book_room(room):
        return {"room": room}

    bookings.execute("INSERT INTO bookings (room) VALUES (4), (4)")
    book_room(4)

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()

    assert only_row(bookings)["status"] == "pending"
    assert "check ambiguous" in caplog.text


def test_custom_check_gets_params_result_and_the_action_row(db):
    seen = {}

    def check(params, result, ctx):
        seen.update(params=params, result=result, type=ctx["action_type"])
        return CheckResult.verified(source="crm")

    @verilays.action(type="send_email", system="crm", verify=check)
    def send_email(to):
        return {"msg_id": "m1"}

    send_email("a@b.c")
    run_once()

    assert seen == {"params": {"to": "a@b.c"}, "result": {"msg_id": "m1"}, "type": "send_email"}
    row = only_row(db)
    assert row["status"] == "verified"
    assert row["evidence"] == {"source": "crm"}


def test_check_that_raises_stays_pending_and_the_next_action_still_runs(db, caplog):
    def broken(params, result, ctx):
        raise ConnectionError("crm is down")

    @verilays.action(type="broken", system="crm", verify=broken)
    def first():
        return None

    @verilays.action(type="fine", system="crm", verify=lambda p, r, c: CheckResult.verified())
    def second():
        return None

    first()
    second()
    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()

    rows = {r["action_type"]: r for r in db.execute("SELECT * FROM verilays_actions")}
    assert rows["broken"]["status"] == "pending"
    assert rows["broken"]["attempts"] == 1
    assert rows["fine"]["status"] == "verified"
    assert "check raised" in caplog.text
    assert "crm is down" in caplog.text


def test_unregistered_type_is_skipped_and_logged(db, caplog):
    store.insert_action(DSN, "gone_tool", "crm", {}, None)

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()

    row = only_row(db)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert "gone_tool skipped: type not registered" in caplog.text


def test_check_returning_a_non_checkresult_stays_pending_and_the_next_action_still_runs(db, caplog):
    @verilays.action(type="sloppy", system="crm", verify=lambda p, r, c: True)
    def first():
        return None

    @verilays.action(type="fine", system="crm", verify=lambda p, r, c: CheckResult.verified())
    def second():
        return None

    first()
    second()
    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()

    rows = {r["action_type"]: r for r in db.execute("SELECT * FROM verilays_actions")}
    assert rows["sloppy"]["status"] == "pending"
    assert rows["sloppy"]["attempts"] == 1
    assert rows["fine"]["status"] == "verified"
    assert "check returned True, not a CheckResult" in caplog.text


def test_result_without_the_lookup_field_stays_pending_with_a_clear_warning(bookings, caplog):
    book_tool(returns_dict=False)(room=4, start_time="10:00")  # returns 1, not {"id": 1}

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once()

    row = only_row(bookings)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "result has no field 'id' (got 1)" in caplog.text
