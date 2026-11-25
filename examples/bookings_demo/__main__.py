"""The v0 demo: a booking agent hits each failure Verilays catches, then the report shows what happened.

    docker compose run --build --rm demo

It starts from empty tables: it recreates `bookings` and clears `verilays_actions`.
"""
import logging
import os
import threading
import time

import psycopg

import verilays
from verilays.report import report
from verilays.worker import run_every

# Nothing listens on port 1, so recording an action fails at once.
DEAD_STORE = "postgresql://verilays:verilays@localhost:1/verilays"


def run(store_dsn, bookings_dsn, deadline="40s", every=3, overwrite_after=8, late_after=15):
    """Run every scenario, check them until the deadline passes, then print the report.

    late_after and overwrite_after must be shorter than deadline, or those rows end missing/verified.
    """
    def conn():
        return psycopg.connect(bookings_dsn, autocommit=True)

    with conn() as c:
        c.execute("DROP TABLE IF EXISTS bookings")
        c.execute("CREATE TABLE bookings (id serial PRIMARY KEY, room text, start_time time)")
    with psycopg.connect(store_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS verilays_actions")
    verilays.init(store_dsn)
    print("Started from empty bookings and verilays_actions tables.\n")

    @verilays.action.postgres(db="bookings", table="bookings", lookup_field="id",
                              fields_to_compare=["room", "start_time"], deadline=deadline)
    def create_booking(room, start_time, bug=None):
        """The agent's tool. bug picks how it goes wrong; it always says it worked."""
        if bug == "noop":
            return {"id": 999}
        if bug == "raises":
            raise RuntimeError("booking API down")
        if bug == "wrong_time":
            start_time = "11:00"
        with conn() as c:
            if bug == "late":
                # The id is handed out now, the row is written later (a slow queue, say).
                booking_id = c.execute("SELECT nextval('bookings_id_seq')").fetchone()[0]
                insert_later = threading.Timer(late_after, _insert, (conn, booking_id, room, start_time))
                insert_later.start()
                return {"id": booking_id}
            booking_id = c.execute("INSERT INTO bookings (room, start_time) VALUES (%s, %s) RETURNING id",
                                   (room, start_time)).fetchone()[0]
        return {"id": booking_id}

    stop = threading.Event()
    worker = threading.Thread(target=run_every, args=(every, stop))
    worker.start()

    print("happy path:   ", create_booking("happy", "10:00"))
    print("silent no-op: ", create_booking("noop", "10:00", bug="noop"), "(nothing written)")
    print("wrong values: ", create_booking("wrong", "10:00", bug="wrong_time"), "(saved 11:00)")
    print("late write:   ", create_booking("late", "10:00", bug="late"), f"(row written in {late_after}s)")
    print("duplicate:    ", create_booking("dup", "10:00"), create_booking("dup", "10:00"))

    overwritten = create_booking("overwritten", "10:00")
    print("overwritten:  ", overwritten, f"(someone moves it to 12:00 in {overwrite_after}s)")
    overwrite_later = threading.Timer(overwrite_after, _move_to_noon, (conn, overwritten["id"]))
    overwrite_later.start()

    try:
        create_booking("raises", "10:00", bug="raises")
    except RuntimeError as e:
        print("tool raises:   the agent got", repr(e))

    # The store is down: the tool still works, and the warning is the only trace.
    verilays._dsn = DEAD_STORE
    try:
        print("store down:   ", create_booking("store-down", "10:00"), "(see the warning; not in the report)")
    finally:
        verilays._dsn = store_dsn

    wait = verilays.parse_duration(deadline).total_seconds() + 2 * every
    print(f"\nWaiting {wait:.0f}s for the deadline, while the worker checks every {every}s...\n")
    time.sleep(wait)
    stop.set()
    worker.join()

    print(report(store_dsn))


def _insert(conn, booking_id, room, start_time):
    with conn() as c:
        c.execute("INSERT INTO bookings (id, room, start_time) VALUES (%s, %s, %s)",
                  (booking_id, room, start_time))


def _move_to_noon(conn, booking_id):
    with conn() as c:
        c.execute("UPDATE bookings SET start_time = '12:00' WHERE id = %s", (booking_id,))


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    run(os.environ["VERILAYS_DSN"], os.environ["VERILAYS_POSTGRES_BOOKINGS"])
