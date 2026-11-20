import importlib
import logging
import os
import sys

import click

import verilays
from verilays.worker import run_once


@click.group()
def main():
    pass


@main.command()
@click.option("--app", help="Module with custom verify= tools, e.g. myapp.tools. Not needed for"
                            " action.postgres.")
@click.option("--once", is_flag=True, help="Check every pending action once, then exit.")
def worker(app, once):
    """Check pending actions. The action store's DSN comes from VERILAYS_DSN or the app's init."""
    if not once:
        # shortcut: only --once for now; --loop comes with #3 (durations and scheduling).
        raise click.UsageError("pass --once (--loop isn't built yet)")
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
    run_once()
