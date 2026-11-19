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
@click.option("--app", required=True, help="Module whose @action tools to check, e.g. myapp.tools.")
@click.option("--once", is_flag=True, help="Check every pending action once, then exit.")
def worker(app, once):
    if not once:
        # shortcut: only --once for now; --loop comes with #3 (durations and scheduling).
        raise click.UsageError("pass --once (--loop isn't built yet)")
    sys.path.insert(0, os.getcwd())
    try:
        importlib.import_module(app)
    except ImportError as e:
        raise click.ClickException(f"can't import --app {app}: {e}")
    if verilays._dsn is None:
        raise click.ClickException(f"no DSN: {app} must call verilays.init(dsn) when imported")
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("verilays").setLevel(logging.INFO)
    run_once()
