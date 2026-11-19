import sys

import pytest
from click.testing import CliRunner

import verilays
from tests.conftest import DSN
from verilays.cli import main

APP = f"""
import verilays
from verilays.checks import CheckResult

verilays.init({DSN!r})

@verilays.action(type="ping", system="test", verify=lambda p, r, c: CheckResult.verified(seen=r))
def ping():
    return "pong"

ping()  # the tool runs and writes a pending row
"""


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", sys.path[:])  # the CLI adds cwd; undo it after the test
    return tmp_path


def worker(app, *extra):
    return CliRunner().invoke(main, ["worker", "--app", app, *extra])


def test_worker_imports_the_app_and_verifies_its_rows(db, app_dir, caplog):
    (app_dir / "cli_app_ok.py").write_text(APP)

    result = worker("cli_app_ok", "--once")

    assert result.exit_code == 0, result.output
    row = db.execute("SELECT * FROM verilays_actions").fetchone()
    assert row["status"] == "verified"
    assert row["evidence"] == {"seen": "pong"}
    assert "→ verified" in caplog.text  # INFO is on without the test setting it


def test_unknown_app_fails_with_a_clear_message(db, app_dir):
    result = worker("no_such_app", "--once")

    assert result.exit_code != 0
    assert "can't import --app no_such_app" in result.output


def test_app_that_never_calls_init_fails_with_a_clear_message(db, app_dir, monkeypatch):
    monkeypatch.setattr(verilays, "_dsn", None)
    (app_dir / "cli_app_no_init.py").write_text("")

    result = worker("cli_app_no_init", "--once")

    assert result.exit_code != 0
    assert "no DSN: cli_app_no_init must call verilays.init(dsn)" in result.output


def test_without_once_fails_and_checks_nothing(db, app_dir):
    (app_dir / "cli_app_no_once.py").write_text(APP)

    result = worker("cli_app_no_once")

    assert result.exit_code != 0
    assert "pass --once" in result.output
    assert db.execute("SELECT count(*) AS n FROM verilays_actions").fetchone()["n"] == 0
