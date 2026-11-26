import asyncio
import dataclasses
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
        "action_id", "run_id", "span_id", "action_type", "system", "entity_key", "params", "result",
        "error", "status", "evidence", "check", "duplicate_of", "attempts", "next_check_at",
        "deadline_at", "created_at", "updated_at",
    }
    assert columns["run_id"] == columns["span_id"] == columns["duplicate_of"] == columns["check"] == "YES"


def test_wrapped_tool_returns_result_unchanged_and_records_one_pending_row(db):
    returned = {"id": 42, "start": datetime.time(10, 0)}

    @verilays.action(tool_name="create_booking", system="postgres:bookings", verify=never_checked)
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
    @verilays.action(tool_name="ping", system="test", verify=never_checked)
    def ping():
        return None

    ping()
    ping()
    assert db.execute("SELECT count(*) AS n FROM verilays_actions").fetchone()["n"] == 2


def test_verify_is_required():
    with pytest.raises(TypeError, match="verify"):
        verilays.action(tool_name="create_booking", system="postgres:bookings")


def test_verify_that_is_not_a_function_fails_at_import():
    with pytest.raises(TypeError, match="verify must be a function"):
        verilays.action(tool_name="create_booking", system="postgres:bookings", verify="oops")


def test_verify_that_cannot_take_params_result_row_fails_at_import():
    with pytest.raises(TypeError):
        verilays.action(tool_name="create_booking", system="postgres:bookings",
                        verify=lambda params, result: None)


def test_tool_that_raises_reraises_the_same_error_and_records_a_failed_row(db):
    @verilays.action(tool_name="create_booking", system="test", verify=never_checked)
    def create_booking(room):
        raise ValueError(f"room {room} taken")

    with pytest.raises(ValueError, match="room 4 taken"):
        create_booking(4)

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "failed"
    assert row["params"] == {"room": 4}
    assert row["error"] == "ValueError('room 4 taken')"


def test_async_tool_returns_its_result_and_records_a_pending_row(db):
    @verilays.action(tool_name="create_booking", system="test", verify=never_checked)
    async def create_booking(room, n=1):
        return {"room": room}

    assert asyncio.run(create_booking(6)) == {"room": 6}

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "pending"
    assert row["params"] == {"room": 6, "n": 1}


def test_async_tool_that_raises_reraises_the_same_error_and_records_a_failed_row(db):
    @verilays.action(tool_name="create_booking", system="test", verify=never_checked)
    async def create_booking(room):
        raise ValueError(f"room {room} taken")

    with pytest.raises(ValueError, match="room 5 taken"):
        asyncio.run(create_booking(5))

    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "ValueError('room 5 taken')"


def test_store_down_tool_still_returns_and_a_warning_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(verilays, "_dsn", "postgresql://x@localhost:1/x")  # closed port

    @verilays.action(tool_name="create_booking", system="test", verify=never_checked)
    def create_booking(room):
        return {"room": room}

    @verilays.action(tool_name="create_booking_async", system="test", verify=never_checked)
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
    assert row["check"] == {"db": "bookings", "table": "bookings", "lookup_field": {"id": "id"},
                            "fields_to_compare": {"room": "room", "start_time": "start_time"},
                            "duplicate_window": "1m",
                            "exists": True, "expected": None}
    assert verilays.registry == {}


def test_tool_name_defaults_to_the_function_name(db):
    @verilays.action.postgres(db="bookings", table="bookings", lookup_field="id")
    def create_booking(room):
        return {"id": 1}

    @verilays.action.postgres("book_room", db="bookings", table="bookings", lookup_field="id")
    def _book_room_impl(room):
        return {"id": 2}

    @verilays.action(system="crm", verify=never_checked)
    def send_email(to):
        return {"id": 3}

    create_booking(4)
    _book_room_impl(4)
    send_email("a@example.com")

    rows = db.execute("SELECT action_type FROM verilays_actions ORDER BY created_at").fetchall()
    assert [row["action_type"] for row in rows] == ["create_booking", "book_room", "send_email"]
    assert "send_email" in verilays.registry


def test_tool_called_before_init_still_returns_and_warns_clearly(monkeypatch, caplog):
    monkeypatch.setattr(verilays, "_dsn", None)

    @verilays.action.postgres("create_booking", db="bookings", table="bookings",
                              lookup_field="id", fields_to_compare=[])
    def create_booking(room):
        return {"id": 1}

    assert create_booking(4) == {"id": 1}
    assert "create_booking not recorded: verilays.init(dsn) was never called" in caplog.text


@pytest.mark.parametrize("text, expected", [
    ("10s", datetime.timedelta(seconds=10)),
    ("5m", datetime.timedelta(minutes=5)),
    ("1h", datetime.timedelta(hours=1)),
])
def test_deadline_strings_parse(text, expected):
    assert verilays.parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["5 min", "1d", "m", "-5m", "", 60])
def test_bad_deadline_is_rejected_by_parse_duration(bad):
    with pytest.raises(ValueError, match="deadline must look like '10s', '5m' or '1h'"):
        verilays.parse_duration(bad)


def test_deadline_is_stored_in_deadline_at_default_1m(db):
    @verilays.action(tool_name="ping", system="test", verify=never_checked)
    def ping():
        return None

    @verilays.action.postgres("slow_sync", db="crm", table="contacts", lookup_field="id",
                              fields_to_compare=[], deadline="1h")
    def slow_sync():
        return {"id": 1}

    ping()
    slow_sync()

    waits = {
        r["action_type"]: r["wait"]
        for r in db.execute("SELECT action_type, deadline_at - created_at AS wait FROM verilays_actions")
    }
    assert waits == {"ping": datetime.timedelta(minutes=1), "slow_sync": datetime.timedelta(hours=1)}


def test_saved_check_fills_missing_keys_and_drops_unknown_ones():
    from verilays.systems.postgres import PostgresCheck

    old = {"db": "bookings", "table": "bookings", "lookup_field": "id", "fields_to_compare": ["room"]}
    newer = {**old, "some_future_option": 1}

    assert PostgresCheck.from_saved(old) == PostgresCheck(**old)  # duplicate_window, exists, expected: defaults
    assert PostgresCheck.from_saved(newer) == PostgresCheck(**old)


@pytest.mark.parametrize("bad, message", [
    ({"lookup_field": 5}, "lookup_field must be str | dict, got 5"),
    ({"lookup_field": {"id": "order_id", "ref": "order_ref"}}, "lookup_field must name one column"),
    ({"fields_to_compare": "room"}, "fields_to_compare must be list | dict, got 'room'"),
    ({"fields_to_compare": {"room": ["id"]}}, "column and argument names must be str, got 'room': ['id']"),
    ({"fields_to_compare": ["reason"]}, "fields_to_compare names ['reason'], which cancel_order doesn't take"),
    ({"fields_to_compare": {"status": "cancelled"}},
     "fields_to_compare names ['cancelled'], which cancel_order doesn't take"),
    ({"exists": "no"}, "exists must be bool, got 'no'"),
    ({"expected": "cancelled"}, "expected must be dict | None, got 'cancelled'"),
    ({"deadline": "5 min"}, "deadline must look like '10s', '5m' or '1h'"),
    ({"duplicate_window": "1d"}, "deadline must look like '10s', '5m' or '1h'"),
])
def test_action_postgres_with_a_bad_setting_still_runs_the_tool_unchecked(db, caplog, bad, message):
    args = {"db": "shop", "table": "orders", "lookup_field": "id", "fields_to_compare": [], **bad}

    @verilays.action.postgres(**args)  # no tool_name: the warning names the function
    def cancel_order(id):
        return "cancelled"

    assert cancel_order(1) == "cancelled"
    assert db.execute("SELECT count(*) AS n FROM verilays_actions").fetchone()["n"] == 0
    assert f"cancel_order runs but is not checked: {message}" in caplog.text


def test_action_postgres_saves_only_the_checked_arguments_and_result_field(db):
    @verilays.action.postgres("refund", db="shop", table="orders", lookup_field="order_id",
                              fields_to_compare=["amount"])
    def refund(order_id, amount, card_number):
        return {"order_id": order_id, "receipt_url": "https://pay.example/r/secret"}

    refund(42, 30, "4111 1111 1111 1111")

    row = db.execute("SELECT params, result FROM verilays_actions").fetchone()
    assert row["params"] == {"order_id": 42, "amount": 30}
    assert row["result"] == {"order_id": 42}


def test_action_postgres_with_mapped_names_saves_only_the_mapped_arguments(db):
    @verilays.action.postgres("refund", db="shop", table="orders", lookup_field={"id": "order_id"},
                              fields_to_compare={"amount_cents": "amount"})
    def refund(order_id, amount, card_number):
        return {"order_id": order_id, "receipt_url": "https://pay.example/r/secret"}

    refund(42, 30, "4111 1111 1111 1111")

    row = db.execute("SELECT params, result FROM verilays_actions").fetchone()
    assert row["params"] == {"order_id": 42, "amount": 30}
    assert row["result"] == {"order_id": 42}


def test_run_id_and_span_id_are_the_ids_of_the_calling_span(db):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    @verilays.action(tool_name="ping", system="test", verify=never_checked)
    def ping():
        return None

    ping()  # outside a trace
    with TracerProvider().get_tracer("test").start_as_current_span("agent run") as span:
        ping()
    span_context = span.get_span_context()
    trace_id = format(span_context.trace_id, "032x")
    span_id = format(span_context.span_id, "016x")

    rows = db.execute("SELECT run_id, span_id FROM verilays_actions ORDER BY created_at").fetchall()
    ids = [(row["run_id"], row["span_id"]) for row in rows]
    assert ids == [(None, None), (trace_id, span_id)]


@dataclasses.dataclass
class RefundState:
    order_id: int
    amount: int
    card_number: str


class PydanticLikeState:
    def __init__(self, **fields):
        self.fields = fields

    def model_dump(self):
        return self.fields


@pytest.mark.parametrize("state", [
    {"order_id": 42, "amount": 30, "card_number": "4111 1111 1111 1111"},
    RefundState(order_id=42, amount=30, card_number="4111 1111 1111 1111"),
    PydanticLikeState(order_id=42, amount=30, card_number="4111 1111 1111 1111"),
])
def test_langgraph_postgres_saves_only_the_checked_fields_of_the_state(db, state):
    @verilays.langgraph.postgres("refund", db="shop", table="orders", lookup_field={"id": "refund_id"},
                                 fields_to_compare=["order_id", "amount"])
    def refund_node(state):
        return {"refund_id": 7, "receipt_url": "https://pay.example/r/secret"}

    assert refund_node(state) == {"refund_id": 7, "receipt_url": "https://pay.example/r/secret"}

    row = db.execute("SELECT params, result FROM verilays_actions").fetchone()
    assert row["params"] == {"order_id": 42, "amount": 30}
    assert row["result"] == {"refund_id": 7}


def test_langgraph_async_node_with_a_custom_verify_records_its_state_fields(db):
    @verilays.langgraph(system="crm", verify=never_checked)
    async def send_email(state, config=None):
        return {"sent": True}

    assert asyncio.run(send_email({"to": "a@example.com"})) == {"sent": True}

    row = db.execute("SELECT params, result FROM verilays_actions").fetchone()
    assert row["params"] == {"to": "a@example.com"}
    assert row["result"] == {"sent": True}


def test_langgraph_node_with_an_unknown_state_still_runs_and_is_not_recorded(db):
    @verilays.langgraph.postgres(db="shop", table="orders", lookup_field="id", fields_to_compare=["amount"])
    def refund_node(state):
        return {"id": 1}

    assert refund_node("not a state") == {"id": 1}
    assert db.execute("SELECT count(*) AS n FROM verilays_actions").fetchone()["n"] == 0
