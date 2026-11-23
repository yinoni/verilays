import importlib
import logging
import os
import signal
import sys
import threading

import click

import verilays
from verilays.report import report as build_report
from verilays.worker import retry_check_errors, run_every, run_once


@click.group()
def main():
    pass


@main.command()
@click.option("--app", help="Module with custom verify= tools, e.g. myapp.tools. Not needed for"
                            " action.postgres.")
@click.option("--once", is_flag=True, help="Check every pending action once, then exit.")
@click.option("--every", type=float, help="Check every N seconds until stopped (Ctrl-C or SIGTERM).")
def worker(app, once, every):
    """Check pending actions. The action store's DSN comes from VERILAYS_DSN or the app's init."""
    if once and every is not None:
        raise click.UsageError("pass --once or --every N, not both")
    if not once and every is None:
        raise click.UsageError("pass --once or --every N")
    if app:
        sys.path.insert(0, os.getcwd())
        try:
            importlib.import_module(app)
        except ImportError as e:
            raise click.ClickException(f"can't import --app {app}: {e}")
    if verilays._dsn is None:
        if "VERILAYS_DSN" not in os.environ:
            raise click.ClickException("no DSN: set VERILAYS_DSN, or have --app call verilays.init(dsn)")
        verilays.init(os.environ["VERILAYS_DSN"])
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("verilays").setLevel(logging.INFO)
    retry_check_errors()
    if once:
        run_once()
        return

    # SIGTERM (docker stop) finishes the current pass, then exits, instead of killing it mid-way.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    run_every(every, stop)


@main.command()
def report():
    """Print action counts per tool and status, then every action that needs attention."""
    if "VERILAYS_DSN" not in os.environ:
        raise click.ClickException("no DSN: set VERILAYS_DSN to the action store's URL")
    click.echo(build_report(os.environ["VERILAYS_DSN"]))
