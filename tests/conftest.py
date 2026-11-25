import os

import psycopg
import pytest
from psycopg.rows import dict_row

import verilays

# docker compose up -d; VERILAYS_TEST_DSN points the tests at another Postgres (they drop its tables).
DSN = os.environ.get("VERILAYS_TEST_DSN", "postgresql://verilays:verilays@localhost:55432/verilays")


@pytest.fixture
def db():
    """A fresh Verilays store; yields a connection for reading the action table."""
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("DROP TABLE IF EXISTS verilays_actions")
        verilays.registry.clear()
        verilays.init(DSN)
        yield conn


@pytest.fixture
def bookings(db):
    """The customer's own table, in the same Postgres."""
    db.execute("DROP TABLE IF EXISTS bookings")
    db.execute("CREATE TABLE bookings (id serial PRIMARY KEY, room int, start_time time)")
    yield db
    db.execute("DROP TABLE bookings")
