import psycopg
import pytest
from psycopg.rows import dict_row

import verilays

# docker compose up -d
DSN = "postgresql://verilays:verilays@localhost:55432/verilays"


@pytest.fixture
def db():
    """A fresh Verilays store; yields a connection for reading the action table."""
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("DROP TABLE IF EXISTS verilays_actions")
        verilays.registry.clear()
        verilays.init(DSN)
        yield conn
