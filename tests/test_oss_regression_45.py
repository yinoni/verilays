import logging
from unittest.mock import Mock

import psycopg

import verilays


def test_action_store_connect_timeout_preserves_tool_result(monkeypatch, caplog):
    monkeypatch.setattr(verilays, "_dsn", "postgresql://localhost/bookings")
    connect = Mock(side_effect=psycopg.errors.ConnectionTimeout("connection timed out"))
    monkeypatch.setattr(psycopg, "connect", connect)
    result = {"id": 42}

    @verilays.action.postgres(db="bookings", table="bookings", lookup_field="id")
    def create_booking():
        return result

    assert create_booking() is result
    assert (
        "verilays", logging.WARNING, "insert record error: connection timed out"
    ) in caplog.record_tuples
    assert connect.call_count == 1
    assert connect.call_args.kwargs.get("connect_timeout") == 5
