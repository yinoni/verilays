import logging

from examples.bookings_demo.__main__ import run
from tests.conftest import DSN


def test_demo_ends_with_each_scenario_status(db, monkeypatch, capsys, caplog):
    monkeypatch.setenv("VERILAYS_POSTGRES_BOOKINGS", DSN)

    with caplog.at_level(logging.WARNING, logger="verilays"):
        run(DSN, DSN, deadline="4s", every=0.5, overwrite_after=1, late_after=2)

    rows = db.execute("SELECT * FROM verilays_actions ORDER BY created_at").fetchall()
    statuses = {row["params"]["room"]: row["status"] for row in rows if row["params"]["room"] != "dup"}
    assert statuses == {"happy": "verified", "noop": "missing", "wrong": "mismatch",
                        "late": "verified", "overwritten": "ambiguous", "raises": "failed"}

    first, second = [row for row in rows if row["params"]["room"] == "dup"]
    assert (first["status"], second["status"]) == ("verified", "verified")
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first["action_id"]

    assert not any(row["params"]["room"] == "store-down" for row in rows)
    assert "insert record error" in caplog.text

    out = capsys.readouterr().out
    assert "Needs attention" in out
    assert "changed after verified: start_time" in out
