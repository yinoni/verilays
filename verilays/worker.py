import contextlib
import datetime
import logging
import os
import uuid

import psycopg
from psycopg.rows import dict_row

import verilays
from verilays.checks import CheckResult
from verilays.recipes import pg_row_matches
from verilays.store import _jsonb

log = logging.getLogger("verilays")


class ConfigError(Exception):
    """The worker is set up wrong (a missing env var, an unknown system); retrying won't help."""


class DatabaseDown(Exception):
    """A checked database already failed to answer in this pass; its rows wait for the next pass."""


# A check is a lookup by key, normally a few ms; past this the database is stuck, not slow.
CHECK_TIMEOUT_SECONDS = 5


def _postgres(check, connections):
    db_name = check['db'].upper()
    env = f"VERILAYS_POSTGRES_{db_name}"

    if env not in os.environ:
        raise ConfigError(f"{env} is not set; set it to the URL of the {check['db']!r} database")

    url = os.environ[env]
    if url in connections and connections[url] is None:
        raise DatabaseDown(f"{check['db']!r} didn't answer earlier in this pass")
    if url not in connections:
        try:
            # autocommit: a failed query (say, a wrong table) must not abort the next row's check.
            connections[url] = psycopg.connect(
                url, autocommit=True, connect_timeout=CHECK_TIMEOUT_SECONDS,
                options=f"-c statement_timeout={CHECK_TIMEOUT_SECONDS}s")
        except psycopg.OperationalError:
            connections[url] = None
            raise
    conn = connections[url]

    # nullcontext: pg_row_matches's `with` uses the pass's connection without closing it.
    check_row = pg_row_matches(lambda: contextlib.nullcontext(conn), check["table"],
                               check["lookup_field"], check["fields_to_compare"])

    def verify(params, result, ctx):
        try:
            return check_row(params, result, ctx)
        except psycopg.OperationalError:
            # Timed out or lost the connection: skip this database's other rows until the next pass,
            # or each one waits the full timeout. A wrong table (ProgrammingError) isn't caught here.
            conn.close()
            connections[url] = None
            raise

    return verify


# One entry per system: turns the check saved in the row into a verify function.
# Each system reads its connection from VERILAYS_<SYSTEM>_<NAME>, opened once per pass and kept
# in `connections` (URL → connection, or None once it failed to answer in this pass).
SYSTEMS = {"postgres": _postgres}


def _verify_from_check(row, connections):
    if row["system"] not in SYSTEMS:
        raise ConfigError(f"unknown system {row['system']!r}; known: {', '.join(SYSTEMS)}")

    fn = SYSTEMS[row["system"]]

    return fn(row["check"], connections)


BATCH_SIZE = 100


def run_once(now=None):
    """Check every pending action once, in batches of BATCH_SIZE, one transaction per batch.

    Found → verified or mismatch. Not found → stays pending until deadline_at, then missing.
    A check that raises keeps it pending until deadline_at, then expired.
    """
    # A row that stays pending must not come back in this pass, so each batch starts after the last one.
    after = (datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), uuid.UUID(int=0))
    connections = {}
    try:
        with psycopg.connect(verilays._dsn, row_factory=dict_row) as conn:
            if now is None:
                # deadline_at was set by the database's clock, so compare with that clock too:
                # a worker whose clock runs ahead would otherwise mark rows missing too early.
                # Its own transaction, or every batch below would nest inside it and keep its locks.
                with conn.transaction():
                    now = conn.execute("SELECT now()").fetchone()["now"]
            while True:
                # The row locks are held until the batch commits, so a second worker skips these rows.
                with conn.transaction():
                    rows = conn.execute(
                        "SELECT * FROM verilays_actions WHERE status = 'pending'"
                        " AND (created_at, action_id) > (%s, %s)"
                        " ORDER BY created_at, action_id LIMIT %s FOR UPDATE SKIP LOCKED",
                        (*after, BATCH_SIZE),
                    ).fetchall()
                    # shortcut: one query per row; batch per table (WHERE id = ANY(...))
                    # when pending rows reach the thousands.
                    for row in rows:
                        _check_row(conn, row, now, connections)
                if len(rows) < BATCH_SIZE:
                    return
                last = rows[-1]
                after = (last["created_at"], last["action_id"])
    finally:
        for checked_db in connections.values():
            if checked_db is not None:
                checked_db.close()


def _check_row(conn, row, now, connections):
    entry = verilays.registry.get(row["action_type"])
    if row["check"] is None and entry is None:
        log.warning("action %s/%s skipped: type not registered",
                    row["action_id"], row["action_type"])
        return

    attempt = row["attempts"] + 1
    past_deadline = now >= row["deadline_at"]
    # Each branch sets what it learned; columns it doesn't touch keep the row's old value.
    error = row["error"]
    evidence = row["evidence"]
    try:
        verify = _verify_from_check(row, connections) if row["check"] else entry["verify"]
        res = verify(row["params"], row["result"], row)
        if not isinstance(res, CheckResult):
            raise TypeError(f"check returned {res!r}, not a CheckResult")
    except DatabaseDown as e:
        log.info("action %s/%s skipped until the next pass: %s",
                 row["action_id"], row["action_type"], e)
        return
    except ConfigError as e:
        status = "check_error"
        error = str(e)
        log.warning("action %s/%s → check_error: %s (fix the worker's config and restart it)",
                    row["action_id"], row["action_type"], e)
    except Exception as e:
        # We never found out, so this is expired, not missing: don't blame the agent.
        status = "expired" if past_deadline else "pending"
        error = repr(e)
        log.warning("action %s/%s check raised → %s (attempt %d)",
                    row["action_id"], row["action_type"], status, attempt, exc_info=True)
    else:
        status = "pending"

        if res.status in ("verified", "mismatch"):
            status = res.status
        elif res.status == "not_found" and past_deadline:
            status = "missing"
        evidence = res.evidence
        log.info("action %s/%s → %s (check %s, attempt %d) evidence=%s",
                 row["action_id"], row["action_type"], status, res.status, attempt,
                 res.evidence)

    conn.execute(
        "UPDATE verilays_actions SET status = %s, error = %s, evidence = %s, attempts = %s,"
        " updated_at = %s WHERE action_id = %s",
        (status, error, _jsonb(evidence) if evidence is not None else None, attempt, now,
         row["action_id"]),
    )


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

