import datetime
import logging
import os

import psycopg
from psycopg.rows import dict_row

import verilays
from verilays.checks import CheckResult
from verilays.recipes import pg_row_matches
from verilays.store import _jsonb

log = logging.getLogger("verilays")


class ConfigError(Exception):
    """The worker is set up wrong (a missing env var, an unknown system); retrying won't help."""


def _postgres(check):
    db_name = check['db'].upper()
    env = f"VERILAYS_POSTGRES_{db_name}"

    if env not in os.environ:
        raise ConfigError(f"{env} is not set; set it to the URL of the {check['db']!r} database")

    url = os.environ[env]

    return pg_row_matches(lambda: psycopg.connect(url), check["table"],
                          check["lookup_field"], check["fields_to_compare"])


# One entry per system: turns the check saved in the row into a verify function.
# Each system reads its connection from VERILAYS_<SYSTEM>_<NAME>.
SYSTEMS = {"postgres": _postgres}


def _verify_from_check(row):
    if row["system"] not in SYSTEMS:
        raise ConfigError(f"unknown system {row['system']!r}; known: {', '.join(SYSTEMS)}")

    fn = SYSTEMS[row["system"]]

    return fn(row["check"])


def run_once(now=None):
    """Check every pending action once; a verified check marks it verified."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    with psycopg.connect(verilays._dsn, row_factory=dict_row) as conn:
        # shortcut: every pending row is due on every run; #3 adds next_check_at scheduling.
        # The row locks are held until all checks finish, so a second worker skips these rows.
        rows = conn.execute(
            "SELECT * FROM verilays_actions WHERE status = 'pending'"
            " ORDER BY created_at FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for row in rows:
            entry = verilays.registry.get(row["action_type"])
            if row["check"] is None and entry is None:
                log.warning("action %s/%s skipped: type not registered",
                            row["action_id"], row["action_type"])
                continue

            attempt = row["attempts"] + 1
            try:
                verify = _verify_from_check(row) if row["check"] else entry["verify"]
                res = verify(row["params"], row["result"], row)
                if not isinstance(res, CheckResult):
                    raise TypeError(f"check returned {res!r}, not a CheckResult")
            except ConfigError as e:
                log.warning("action %s/%s → check_error: %s (fix the worker's config and restart it)",
                            row["action_id"], row["action_type"], e)
                conn.execute(
                    "UPDATE verilays_actions SET status = 'check_error', error = %s, attempts = %s,"
                    " updated_at = %s WHERE action_id = %s",
                    (str(e), attempt, now, row["action_id"]),
                )
                continue
            except Exception:
                log.warning("action %s/%s check raised, stays pending (attempt %d)",
                            row["action_id"], row["action_type"], attempt, exc_info=True)
                conn.execute(
                    "UPDATE verilays_actions SET attempts = %s, updated_at = %s WHERE action_id = %s",
                    (attempt, now, row["action_id"]),
                )
                continue

            # shortcut: not_found/ambiguous stay pending and are retried; #5 adds those transitions.
            status = res.status if res.status in ("verified", "mismatch") else "pending"
            conn.execute(
                "UPDATE verilays_actions SET status = %s, evidence = %s, attempts = %s,"
                " updated_at = %s WHERE action_id = %s",
                (status, _jsonb(res.evidence), attempt, now, row["action_id"]),
            )
            log.info("action %s/%s → %s (check %s, attempt %d) evidence=%s",
                     row["action_id"], row["action_type"], status, res.status, attempt,
                     res.evidence)


def retry_check_errors():
    """Move check_error rows back to pending. Called when the worker starts: its config may be fixed now."""
    with psycopg.connect(verilays._dsn) as conn:
        count = conn.execute(
            "UPDATE verilays_actions SET status = 'pending', error = NULL, updated_at = now()"
            " WHERE status = 'check_error'"
        ).rowcount
    if count:
        log.info("%d check_error action(s) back to pending for a new try", count)


def run_every(seconds, stop):
    """Run a check pass every `seconds` until `stop` (a threading.Event) is set.

    A failed pass (e.g. the store is down) is logged and tried again next time.
    """
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            log.warning("check pass failed, retrying in %ss", seconds, exc_info=True)
        stop.wait(seconds)

