import datetime
import logging
import threading
import time
import types

import psycopg
import pytest

import verilays
from tests.conftest import DSN
from verilays import recipes, store
from verilays.checks import CheckResult
from verilays import worker
from verilays.worker import run_every, run_once


def bookings_conn():
    return psycopg.connect(DSN)


def book_tool(writes=True, returns_dict=True):
    """A create_booking tool that claims success; with writes=False it never saves."""

    @verilays.action(
        tool_name="create_booking",
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
    assert row["evidence"] == {"key": {"id": 2}}
    assert "→ verified (check verified, attempt 1)" in caplog.text
    assert "evidence={'key': {'id': 2}}" in caplog.text  # which row; its values aren't logged


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


def test_wrong_value_is_a_mismatch_with_a_field_diff(bookings):
    book_tool()(room=4, start_time="10:00")
    bookings.execute("UPDATE bookings SET start_time = '11:00'")

    run_once()

    row = only_row(bookings)
    assert row["status"] == "mismatch"
    assert row["evidence"]["diff"] == {
        "start_time": {"expected": "10:00", "got": "11:00:00"}
    }


def test_two_matching_rows_are_ambiguous_and_stay_pending(bookings, caplog):
    @verilays.action(
        tool_name="book_room",
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

    @verilays.action(tool_name="send_email", system="crm", verify=check)
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

    @verilays.action(tool_name="broken", system="crm", verify=broken)
    def first():
        return None

    @verilays.action(tool_name="fine", system="crm", verify=lambda p, r, c: CheckResult.verified())
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
    @verilays.action(tool_name="sloppy", system="crm", verify=lambda p, r, c: True)
    def first():
        return None

    @verilays.action(tool_name="fine", system="crm", verify=lambda p, r, c: CheckResult.verified())
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
    assert "neither result nor params has field 'id' (result was 1)" in caplog.text


def test_lookup_field_falls_back_to_params_when_the_result_lacks_it(bookings):
    bookings.execute("INSERT INTO bookings (room, start_time) VALUES (4, '10:00')")  # id 1

    @verilays.action(
        tool_name="move_booking",
        system="postgres:bookings",
        verify=recipes.pg_row_matches(bookings_conn, "bookings", lookup_field="id", fields_to_compare=["room"]),
    )
    def move_booking(id, room):
        with bookings_conn() as conn:
            conn.execute("UPDATE bookings SET room = %s WHERE id = %s", (room, id))
        return True  # no {"id": ...}, so the id comes from params

    move_booking(id=1, room=7)
    run_once()

    assert only_row(bookings)["status"] == "verified"


def postgres_tool(fields_to_compare=("room", "start_time"), **kwargs):
    """A create_booking tool that saves its check as data; no verify function, nothing in registry."""

    @verilays.action.postgres("create_booking", db="bookings", table="bookings",
                              lookup_field="id", fields_to_compare=list(fields_to_compare), **kwargs)
    def create_booking(room, start_time):
        with bookings_conn() as conn:
            row = conn.execute(
                "INSERT INTO bookings (room, start_time) VALUES (%s, %s) RETURNING id",
                (room, start_time),
            ).fetchone()
        return {"id": row[0]}

    return create_booking


def test_action_postgres_is_verified_with_only_the_db_env_var(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    postgres_tool()(room=4, start_time="10:00")

    run_once()

    row = only_row(bookings)
    assert row["status"] == "verified"
    assert row["evidence"] == {"key": {"id": 1}}


def test_checked_database_connection_refuses_writes(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    postgres_tool()(room=4, start_time="10:00")
    connections = {}

    worker._postgres(only_row(bookings)["check"], connections)

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        connections[DSN].execute("INSERT INTO bookings (room) VALUES (5)")
    connections[DSN].close()


def test_exists_false_is_verified_once_the_row_is_gone_and_mismatch_if_it_stays(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    bookings.execute("INSERT INTO bookings (room, start_time) VALUES (4, '10:00'), (5, '11:00')")  # ids 1, 2

    @verilays.action.postgres("delete_booking", db="bookings", table="bookings",
                              lookup_field="id", fields_to_compare=[], exists=False)
    def delete_booking(id, really=True):
        if really:
            with bookings_conn() as conn:
                conn.execute("DELETE FROM bookings WHERE id = %s", (id,))
        return True

    delete_booking(id=1)
    delete_booking(id=2, really=False)  # says it deleted, row is still there
    deadline = max(r["deadline_at"] for r in bookings.execute("SELECT deadline_at FROM verilays_actions"))

    run_once(now=deadline - SECOND)
    by_id = lambda: {r["params"]["id"]: r for r in bookings.execute("SELECT * FROM verilays_actions")}
    assert by_id()[1]["status"] == "verified"
    assert by_id()[2]["status"] == "pending"

    run_once(now=deadline)
    assert by_id()[2]["status"] == "mismatch"
    assert by_id()[2]["evidence"] == {"key": {"id": 2}, "still_exists": True}


def test_expected_values_win_over_params_for_the_same_field(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    bookings.execute("INSERT INTO bookings (room, start_time) VALUES (1, '10:00'), (1, '11:00')")  # ids 1, 2

    @verilays.action.postgres("move_to_hall", db="bookings", table="bookings", lookup_field="id",
                              fields_to_compare=["room"], expected={"room": 9})
    def move_to_hall(id, room, works=True):  # room is only a hint; the hall is always room 9
        with bookings_conn() as conn:
            conn.execute("UPDATE bookings SET room = %s WHERE id = %s", (9 if works else room, id))
        return True

    move_to_hall(id=1, room=4)
    move_to_hall(id=2, room=4, works=False)
    run_once()

    by_id = {r["params"]["id"]: r for r in bookings.execute("SELECT * FROM verilays_actions")}
    assert by_id[1]["status"] == "verified"
    assert by_id[2]["status"] == "mismatch"
    assert by_id[2]["evidence"]["diff"] == {"room": {"expected": 9, "got": "4"}}


def test_missing_db_env_var_is_a_check_error_and_is_not_retried(bookings, monkeypatch, caplog):
    monkeypatch.delenv("VERILAYS_POSTGRES_BOOKINGS", raising=False)
    postgres_tool()(room=4, start_time="10:00")

    run_once()
    run_once()

    row = only_row(bookings)
    assert row["status"] == "check_error"
    assert row["attempts"] == 1
    assert row["error"] == ("VERILAYS_POSTGRES_BOOKINGS is not set; "
                            "set it to the URL of the 'bookings' database")
    assert "→ check_error" in caplog.text


def test_check_for_an_unknown_system_is_a_check_error(db, caplog):
    store.insert_action(DSN, "charge", "stripe", {}, None, check={"amount": 5})

    run_once()

    row = only_row(db)
    assert row["status"] == "check_error"
    assert row["error"] == "unknown system 'stripe'; known: postgres"


def test_run_every_keeps_going_after_a_failed_pass_until_stopped(monkeypatch, caplog):
    stop = threading.Event()
    passes = []

    def fake_run_once():
        passes.append(1)
        if len(passes) == 1:
            raise ConnectionError("store is down")
        stop.set()

    monkeypatch.setattr(worker, "run_once", fake_run_once)

    run_every(0.01, stop)

    assert len(passes) == 2
    assert "check pass failed" in caplog.text
    assert "store is down" in caplog.text


SECOND = datetime.timedelta(seconds=1)


def test_silent_no_op_is_pending_before_the_deadline_and_missing_after(bookings, caplog):
    book_tool(writes=False)(room=4, start_time="10:00")  # says {"id": 999}, wrote nothing
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - SECOND)
    assert only_row(bookings)["status"] == "pending"

    with caplog.at_level(logging.INFO, logger="verilays"):
        run_once(now=deadline)

    row = only_row(bookings)
    assert row["status"] == "missing"
    assert row["evidence"] == {"key": {"id": 999}}
    assert "→ missing (check not_found, attempt 2)" in caplog.text


def test_late_write_before_the_deadline_is_verified(bookings):
    book_tool(writes=False)(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    bookings.execute("INSERT INTO bookings (id, room, start_time) VALUES (999, 4, '10:00')")
    run_once(now=deadline - 20 * SECOND)

    assert only_row(bookings)["status"] == "verified"


def test_mismatch_fixed_before_the_deadline_is_verified(bookings):
    book_tool()(room=4, start_time="10:00")
    bookings.execute("UPDATE bookings SET start_time = '11:00'")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    assert only_row(bookings)["status"] == "mismatch"

    bookings.execute("UPDATE bookings SET start_time = '10:00'")
    run_once(now=deadline - 20 * SECOND)

    assert only_row(bookings)["status"] == "verified"


def test_mismatch_still_wrong_at_the_deadline_stays_mismatch(bookings):
    book_tool()(room=4, start_time="10:00")
    bookings.execute("UPDATE bookings SET start_time = '11:00'")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    run_once(now=deadline)                  # not checked again: the deadline has passed
    bookings.execute("UPDATE bookings SET start_time = '10:00'")
    run_once(now=deadline + SECOND)

    row = only_row(bookings)
    assert row["status"] == "mismatch"
    assert row["attempts"] == 1


def test_verified_is_rechecked_until_the_deadline(bookings):
    book_tool()(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    run_once(now=deadline - 20 * SECOND)
    run_once(now=deadline)                  # not checked again: the deadline has passed

    row = only_row(bookings)
    assert row["status"] == "verified"
    assert row["attempts"] == 2


def test_verified_then_changed_is_ambiguous_with_the_diff(bookings):
    book_tool()(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    bookings.execute("UPDATE bookings SET start_time = '11:00'")  # someone else moves it
    run_once(now=deadline - 20 * SECOND)

    row = only_row(bookings)
    assert row["status"] == "ambiguous"
    assert row["evidence"] == {
        "changed_after_verify": True,
        "key": {"id": 1},
        "diff": {"start_time": {"expected": "10:00", "got": "11:00:00"}},
    }


def test_verified_then_deleted_is_ambiguous(bookings):
    book_tool()(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    bookings.execute("DELETE FROM bookings")
    run_once(now=deadline - 20 * SECOND)

    row = only_row(bookings)
    assert row["status"] == "ambiguous"
    assert row["evidence"] == {"changed_after_verify": True, "key": {"id": 1}}


def test_ambiguous_is_final_even_if_the_row_is_changed_back(bookings):
    book_tool()(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    bookings.execute("UPDATE bookings SET start_time = '11:00'")
    run_once(now=deadline - 20 * SECOND)
    bookings.execute("UPDATE bookings SET start_time = '10:00'")
    run_once(now=deadline - 10 * SECOND)
    run_once(now=deadline + SECOND)

    row = only_row(bookings)
    assert row["status"] == "ambiguous"
    assert row["attempts"] == 2


def test_config_error_on_a_verified_action_keeps_it_verified(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    postgres_tool()(room=4, start_time="10:00")
    run_once()

    monkeypatch.delenv("VERILAYS_POSTGRES_BOOKINGS")
    run_once()

    row = only_row(bookings)
    assert row["status"] == "verified"  # not check_error: a worker restart would reset that to pending
    assert "VERILAYS_POSTGRES_BOOKINGS is not set" in row["error"]


def test_check_that_raises_keeps_a_provisional_mismatch(db):
    crm = {"down": False}

    def verify(params, result, ctx):
        if crm["down"]:
            raise ConnectionError("crm is down")
        return CheckResult.mismatch()

    @verilays.action(tool_name="notify", system="crm", verify=verify)
    def notify():
        return None

    notify()
    deadline = only_row(db)["deadline_at"]

    run_once(now=deadline - 30 * SECOND)
    crm["down"] = True
    run_once(now=deadline - 20 * SECOND)

    row = only_row(db)
    assert row["status"] == "mismatch"
    assert row["error"] == "ConnectionError('crm is down')"


def test_check_that_always_raises_is_expired_after_the_deadline_never_missing(db):
    def broken(params, result, ctx):
        raise ConnectionError("crm is down")

    @verilays.action(tool_name="broken", system="crm", verify=broken)
    def send():
        return None

    send()
    deadline = only_row(db)["deadline_at"]

    run_once(now=deadline - SECOND)
    assert only_row(db)["status"] == "pending"

    run_once(now=deadline)
    row = only_row(db)
    assert row["status"] == "expired"
    assert row["error"] == "ConnectionError('crm is down')"


def test_check_error_does_not_expire(bookings, monkeypatch):
    monkeypatch.delenv("VERILAYS_POSTGRES_BOOKINGS", raising=False)
    postgres_tool()(room=4, start_time="10:00")
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline - SECOND)
    run_once(now=deadline + datetime.timedelta(hours=1))

    assert only_row(bookings)["status"] == "check_error"


def test_failed_tool_stays_failed_after_the_deadline(bookings):
    @verilays.action(tool_name="create_booking", system="crm", verify=lambda p, r, c: CheckResult.verified())
    def create_booking():
        raise RuntimeError("api down")

    with pytest.raises(RuntimeError):
        create_booking()
    deadline = only_row(bookings)["deadline_at"]

    run_once(now=deadline)

    assert only_row(bookings)["status"] == "failed"


def test_final_verdicts_never_change(bookings):
    statuses = {"room 1": "verified", "room 2": "mismatch", "room 3": "missing", "room 4": "expired"}
    book_tool()(room=1, start_time="10:00")                 # verified
    book_tool()(room=2, start_time="10:00")                 # mismatch below
    book_tool(writes=False)(room=3, start_time="10:00")     # says id 999, missing
    bookings.execute("UPDATE bookings SET start_time = '11:00' WHERE room = 2")

    @verilays.action(tool_name="notify", system="crm", verify=lambda p, r, c: 1 / 0)
    def broken(room):
        return None

    broken(room=4)                                          # check raises, expired
    deadline = max(r["deadline_at"] for r in bookings.execute("SELECT deadline_at FROM verilays_actions"))
    run_once(now=deadline)

    def by_room():
        return {f"room {r['params']['room']}": r["status"]
                for r in bookings.execute("SELECT params, status FROM verilays_actions")}

    assert by_room() == statuses

    # Everything that made those verdicts is now fixed; a later pass must not touch them.
    bookings.execute("UPDATE bookings SET start_time = '10:00' WHERE room = 2")
    bookings.execute("INSERT INTO bookings (id, room, start_time) VALUES (999, 3, '10:00')")
    run_once(now=deadline + datetime.timedelta(hours=1))

    assert by_room() == statuses


def many_pings(db, count, verify):
    """Registers a ping tool and inserts `count` pending pings straight into the store."""
    @verilays.action(tool_name="ping", system="test", verify=verify)
    def ping():
        return None

    db.execute(
        "INSERT INTO verilays_actions (action_type, system, params, status, deadline_at)"
        " SELECT 'ping', 'test', '{}', 'pending', now() + interval '1 minute' FROM generate_series(1, %s)",
        (count,),
    )


def test_one_pass_checks_every_row_across_several_batches(db):
    many_pings(db, 250, verify=lambda p, r, c: CheckResult.not_found())  # all stay pending

    run_once()

    attempts = [r["attempts"] for r in db.execute("SELECT attempts FROM verilays_actions")]
    assert attempts == [1] * 250  # every row once, none twice


def test_one_pass_checks_every_provisional_mismatch_once(db):
    many_pings(db, 250, verify=lambda p, r, c: CheckResult.mismatch())
    db.execute("UPDATE verilays_actions SET status = 'mismatch'")

    run_once()

    attempts = [r["attempts"] for r in db.execute("SELECT attempts FROM verilays_actions")]
    assert attempts == [1] * 250  # every row once, none twice


def test_two_workers_check_disjoint_rows(db):
    checked = {"A": [], "B": []}
    a_holds_its_batch = threading.Event()
    b_done = threading.Event()

    def verify(params, result, row):
        worker_name = threading.current_thread().name
        checked[worker_name].append(row["action_id"])
        if worker_name == "A" and not b_done.is_set():
            a_holds_its_batch.set()  # A has locked its first 100 rows
            b_done.wait(5)
        return CheckResult.verified()

    many_pings(db, 150, verify)
    a = threading.Thread(target=run_once, name="A")
    a.start()
    a_holds_its_batch.wait(5)
    b = threading.Thread(target=run_once, name="B")
    b.start()
    b.join(5)
    b_finished_while_a_held_its_rows = not b.is_alive()
    b_done.set()
    a.join(5)

    assert b_finished_while_a_held_its_rows  # B skipped A's locked rows instead of waiting
    a_first_batch = checked["A"][:100]
    assert len(checked["B"]) == 50
    assert not set(a_first_batch) & set(checked["B"])
    assert len(checked["A"]) == 150  # A's second batch rechecks B's verified rows: they're before deadline_at
    statuses = {r["status"] for r in db.execute("SELECT status FROM verilays_actions")}
    assert statuses == {"verified"}


def test_one_connection_per_database_per_pass(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    create_booking = postgres_tool()
    for room in (1, 2, 3):
        create_booking(room=room, start_time="10:00")

    opened = []
    real_connect = psycopg.connect

    def counting_connect(*args, **kwargs):
        opened.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", counting_connect)
    run_once()

    assert len(opened) == 2  # the store, and the bookings DB once for all 3 rows
    statuses = [r["status"] for r in bookings.execute("SELECT status FROM verilays_actions")]
    assert statuses == ["verified"] * 3


def test_a_failed_query_does_not_break_the_next_check_on_the_same_database(bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)

    @verilays.action.postgres("wrong_table", db="bookings", table="no_such_table",
                              lookup_field="id", fields_to_compare=[])
    def wrong_table():
        return {"id": 1}

    wrong_table()
    postgres_tool()(room=4, start_time="10:00")

    run_once()

    rows = {r["action_type"]: r for r in bookings.execute("SELECT * FROM verilays_actions")}
    assert rows["wrong_table"]["status"] == "pending"
    assert "no_such_table" in rows["wrong_table"]["error"]
    assert rows["create_booking"]["status"] == "verified"


def test_a_check_that_raises_leaves_evidence_null(db):
    @verilays.action(tool_name="broken", system="crm", verify=lambda p, r, c: 1 / 0)
    def broken():
        return None

    broken()
    run_once()

    assert db.execute("SELECT evidence IS NULL AS empty FROM verilays_actions").fetchone()["empty"]


def test_a_check_that_raises_keeps_the_evidence_of_the_earlier_check(bookings):
    book_tool(writes=False)(room=4, start_time="10:00")
    run_once()  # not found: evidence {"key": {"id": 999}}
    bookings.execute("ALTER TABLE bookings RENAME TO bookings_gone")  # the next check raises

    try:
        run_once()
    finally:
        bookings.execute("ALTER TABLE bookings_gone RENAME TO bookings")

    row = only_row(bookings)
    assert "bookings" in row["error"]
    assert row["evidence"] == {"key": {"id": 999}}


def test_deadline_uses_the_database_clock_not_the_workers(bookings, monkeypatch):
    class OneHourAhead(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.now(tz) + datetime.timedelta(hours=1)

    monkeypatch.setattr(worker, "datetime",
                        types.SimpleNamespace(datetime=OneHourAhead, timezone=datetime.timezone))
    book_tool(writes=False)(room=4, start_time="10:00")  # deadline: 1m from now, by the DB's clock

    run_once()

    assert only_row(bookings)["status"] == "pending"


def test_each_batch_commits_before_the_next_one_starts(db):
    checked = []
    verified_when_batch_2_started = []

    def verify(params, result, row):
        checked.append(row["action_id"])
        if len(checked) == 101:  # first row of batch 2
            count = db.execute("SELECT count(*) AS n FROM verilays_actions WHERE status = 'verified'")
            verified_when_batch_2_started.append(count.fetchone()["n"])
        return CheckResult.verified()

    many_pings(db, 150, verify)

    run_once()

    assert verified_when_batch_2_started == [100]


def test_a_hanging_query_times_out_and_the_rest_of_that_database_waits_for_the_next_pass(
        bookings, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    monkeypatch.setattr(worker, "CHECK_TIMEOUT_SECONDS", 1)
    create_booking = postgres_tool()
    create_booking(room=1, start_time="10:00")
    create_booking(room=2, start_time="10:00")

    with psycopg.connect(DSN) as migration:
        migration.execute("LOCK TABLE bookings IN ACCESS EXCLUSIVE MODE")  # every SELECT on it waits
        # If the worker had no timeout it would wait for this lock; release it after 10s so the
        # test fails on the asserts instead of hanging.
        safety_release = threading.Timer(10, migration.rollback)
        safety_release.start()
        run_once()
        safety_release.cancel()
        migration.rollback()

    rows = {r["params"]["room"]: r for r in bookings.execute("SELECT * FROM verilays_actions")}
    assert rows[1]["status"] == "pending"
    assert "statement timeout" in rows[1]["error"]
    assert rows[2]["attempts"] == 0  # not tried: its database already timed out in this pass

    run_once()  # the lock is gone

    statuses = [r["status"] for r in bookings.execute("SELECT status FROM verilays_actions")]
    assert statuses == ["verified", "verified"]


def test_a_database_that_never_answers_times_out_once_per_pass(db, monkeypatch):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", "postgresql://u@10.255.255.1/bookings")
    monkeypatch.setattr(worker, "CHECK_TIMEOUT_SECONDS", 2)  # libpq's smallest connect_timeout
    for _ in range(2):
        store.insert_action(DSN, "create_booking", "postgres", {"room": 4}, {"id": 1},
                            check={"db": "bookings", "table": "bookings", "lookup_field": "id",
                                   "fields_to_compare": []})

    started = time.monotonic()
    run_once()

    assert time.monotonic() - started < 5  # without connect_timeout the OS waits ~75s
    attempts = sorted(r["attempts"] for r in db.execute("SELECT attempts FROM verilays_actions"))
    assert attempts == [0, 1]  # the first row timed out, the second wasn't tried


def book_twice(bookings, monkeypatch, gap, second=(4, "10:00"), first_failed=False, **tool_args):
    """Book room 4 at 10:00, then `second`; the first action is moved `gap` earlier. Returns both rows after one pass."""
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)
    tool = postgres_tool(**tool_args)
    tool(4, "10:00")
    tool(*second)
    first_id = bookings.execute(
        "SELECT action_id FROM verilays_actions ORDER BY created_at LIMIT 1").fetchone()["action_id"]
    bookings.execute("UPDATE verilays_actions SET created_at = created_at - %s WHERE action_id = %s",
                     (gap, first_id))
    if first_failed:
        bookings.execute("UPDATE verilays_actions SET status = 'failed' WHERE action_id = %s", (first_id,))

    run_once()

    return bookings.execute("SELECT * FROM verilays_actions ORDER BY created_at").fetchall()


def test_same_booking_within_the_window_is_a_duplicate_of_the_first(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(seconds=30))

    assert (a["status"], b["status"]) == ("verified", "verified")
    assert a["duplicate_of"] is None
    assert b["duplicate_of"] == a["action_id"]


def test_same_booking_outside_the_window_is_not_a_duplicate(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(minutes=2))

    assert b["duplicate_of"] is None


def test_different_room_is_not_a_duplicate(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(seconds=30), second=(5, "10:00"))

    assert b["duplicate_of"] is None


def test_retry_after_a_failed_booking_is_not_a_duplicate(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(seconds=30), first_failed=True)

    assert b["duplicate_of"] is None


def test_no_fields_to_compare_is_never_a_duplicate(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(seconds=30), fields_to_compare=[])

    assert b["duplicate_of"] is None


def test_duplicate_window_is_set_per_action(bookings, monkeypatch):
    a, b = book_twice(bookings, monkeypatch, gap=datetime.timedelta(minutes=2), duplicate_window="5m")

    assert b["duplicate_of"] == a["action_id"]
