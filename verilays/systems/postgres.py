import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, fields

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from verilays.checks import CheckResult, ConfigError, DatabaseDown

VERILAYS_POSTGRES_ENV = "VERILAYS_POSTGRES"

def by_column(names):
    """"id" or ["room"] → {"id": "id"} or {"room": "room"}: the column and the argument share a name.
    A dict {column: argument} is already in this shape.
    """
    if isinstance(names, dict):
        return names
    if isinstance(names, str):
        return {names: names}
    return {name: name for name in names}


@dataclass(kw_only=True)
class PostgresCheck:
    """What action.postgres saves in the action's `check`, and what the worker reads back."""
    table: str
    db: str | None = None
    lookup_field: str | dict
    fields_to_compare: list | dict
    duplicate_window: str = "1m"
    exists: bool = True
    expected: dict | None = None

    def __post_init__(self):
        # Type hints only warn in the editor; this makes a wrong type fail when the tool's file loads.
        for f in fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, f.type):
                raise TypeError(f"{f.name} must be {getattr(f.type, '__name__', f.type)}, got {value!r}")
        # Saved and read back as {column: argument}, whichever form was passed.
        self.lookup_field = by_column(self.lookup_field)
        if len(self.lookup_field) != 1:
            raise ValueError(f"lookup_field must name one column, got {self.lookup_field!r}")
        self.fields_to_compare = by_column(self.fields_to_compare)
        for column, argument in {**self.lookup_field, **self.fields_to_compare}.items():
            if not isinstance(column, str) or not isinstance(argument, str):
                raise TypeError(f"column and argument names must be str, got {column!r}: {argument!r}")

    @classmethod
    def from_saved(cls, saved):
        """Older rows miss newer keys (the defaults fill them); a newer Verilays's extra keys are dropped."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in saved.items() if k in known})


def pg_row_matches(connect: Callable, table: str, lookup_field: str | dict, fields_to_compare: list | dict,
                   exists: bool = True, expected: dict | None = None) -> Callable:
    """Check that exactly one row in `table` matches `lookup_field` and has the `fields_to_compare` values.

    lookup_field: "id" finds the row where column id = result["id"], or params["id"]
    when the result has no "id" (an update or cancel tool that was given the id).
    {"id": "booking_id"} finds column id = result["booking_id"] (or params["booking_id"]).
    fields_to_compare: ["room"] checks that column room = params["room"];
    {"room_id": "room"} checks that column room_id = params["room"].
    expected: {"status": "cancelled"} checks column status = "cancelled", for a value the tool
    sets that isn't one of its arguments. A field in both wins from expected.
    exists=False: for a delete tool. Verified once the row is gone; while it's still there
    the action stays pending until the deadline, then the worker marks it mismatch.
    """
    def check(params, result, ctx):
        # Read here, not when pg_row_matches is called: a bad setting must fail in the worker, not the agent.
        [(lookup_column, lookup_name)] = by_column(lookup_field).items()
        compare = by_column(fields_to_compare)

        def lookup_value(name):
            if isinstance(result, dict) and name in result:
                return result[name]
            if name in params:
                return params[name]
            raise LookupError(f"neither result nor params has field {name!r} (result was {result!r})")

        lookup = lookup_value(lookup_name)
        key = {lookup_column: lookup}
        want = {**{column: params[argument] for column, argument in compare.items()}, **(expected or {})}
        # Postgres does the comparing, so "10:00" matches a time column holding 10:00:00.
        # Each field comes back twice: room (the DB value) and room_matches (True/False).
        query = sql.SQL("SELECT {cols} FROM {table} WHERE {where} LIMIT 2").format(
            cols=sql.SQL(", ").join(
                sql.SQL("{c}::text AS {c}, {c} IS NOT DISTINCT FROM %s AS {m}").format(
                    c=sql.Identifier(f), m=sql.Identifier(f + "_matches"))
                for f in want
            ) if want else sql.SQL("1"),
            table=sql.Identifier(table),
            where=sql.SQL("{} = %s").format(sql.Identifier(lookup_column)),
        )
        with connect() as conn:
            rows = conn.cursor(row_factory=dict_row).execute(
                query, [*want.values(), lookup]
            ).fetchall()

        if not exists:
            return CheckResult.not_found(key=key, still_exists=True) if rows else CheckResult.verified(key=key)
        if not rows:
            return CheckResult.not_found(key=key)
        if len(rows) > 1:
            return CheckResult.ambiguous(key=key, rows=">1")
        row = rows[0]
        diff = {}
        for f in want:
            if not row[f + "_matches"]:
                diff[f] = {"expected": want[f], "got": row[f]}

        if diff:
            return CheckResult.mismatch(key=key, diff=diff)
        return CheckResult.verified(key=key)  # no row values: they'd only repeat what matched

    return check


# A check is a lookup by key, normally a few ms; past this the database is stuck, not slow.
CHECK_TIMEOUT_SECONDS = 5


def verify_from_saved(saved_check, connections):
    """The worker's entry: the saved PostgresCheck → a verify function on this pass's connection."""
    check = PostgresCheck.from_saved(saved_check)
    # db names one of several databases; without it, the one database is in VERILAYS_POSTGRES.

    env = VERILAYS_POSTGRES_ENV

    if check.db is not None:
        env = f"{VERILAYS_POSTGRES_ENV}_{check.db.upper()}"

    if env not in os.environ:
        raise ConfigError(f"{env} is not set; set it to the URL of the database to check")

    url = os.environ[env]
    if url in connections and connections[url] is None:
        raise DatabaseDown(f"the database in {env} didn't answer earlier in this pass")
    if url not in connections:
        try:
            # autocommit: a failed query (say, a wrong table) must not abort the next row's check.
            # read_only: Postgres refuses writes here even if the URL's user is allowed to write.
            connections[url] = psycopg.connect(
                url, autocommit=True, connect_timeout=CHECK_TIMEOUT_SECONDS,
                options=f"-c statement_timeout={CHECK_TIMEOUT_SECONDS}s -c default_transaction_read_only=on")
        except psycopg.OperationalError:
            connections[url] = None
            raise
    conn = connections[url]

    # nullcontext: pg_row_matches's `with` uses the pass's connection without closing it.
    check_row = pg_row_matches(lambda: contextlib.nullcontext(conn), check.table, check.lookup_field,
                               check.fields_to_compare, check.exists, check.expected)

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
