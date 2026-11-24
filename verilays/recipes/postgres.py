from psycopg import sql
from psycopg.rows import dict_row

from verilays.checks import CheckResult


def pg_row_matches(connect, table, lookup_field, fields_to_compare, exists=True, expected=None):
    """Check that exactly one row in `table` matches `lookup_field` and has the `fields_to_compare` values.

    lookup_field: "id" finds the row where column id = result["id"], or params["id"]
    when the result has no "id" (an update or cancel tool that was given the id).
    fields_to_compare: ["room"] checks that column room = params["room"].
    expected: {"status": "cancelled"} checks column status = "cancelled", for a value the tool
    sets that isn't one of its arguments. A field in both wins from expected.
    exists=False: for a delete tool. Verified once the row is gone; while it's still there
    the action stays pending until the deadline, then the worker marks it mismatch.
    """
    def check(params, result, ctx):
        def lookup_value(name):
            if isinstance(result, dict) and name in result:
                return result[name]
            if name in params:
                return params[name]
            raise LookupError(f"neither result nor params has field {name!r} (result was {result!r})")

        key = {lookup_field: lookup_value(lookup_field)}
        want = {**{f: params[f] for f in fields_to_compare}, **(expected or {})}
        # Postgres does the comparing, so "10:00" matches a time column holding 10:00:00.
        # Each field comes back twice: room (the DB value) and room_matches (True/False).
        query = sql.SQL("SELECT {cols} FROM {table} WHERE {where} LIMIT 2").format(
            cols=sql.SQL(", ").join(
                sql.SQL("{c}::text AS {c}, {c} IS NOT DISTINCT FROM %s AS {m}").format(
                    c=sql.Identifier(f), m=sql.Identifier(f + "_matches"))
                for f in want
            ) if want else sql.SQL("1"),
            table=sql.Identifier(table),
            where=sql.SQL("{} = %s").format(sql.Identifier(lookup_field)),
        )
        with connect() as conn:
            rows = conn.cursor(row_factory=dict_row).execute(
                query, [*want.values(), key[lookup_field]]
            ).fetchall()

        if not exists:
            return CheckResult.not_found(key=key, still_exists=True) if rows else CheckResult.verified(key=key)
        if not rows:
            return CheckResult.not_found(key=key)
        if len(rows) > 1:
            return CheckResult.ambiguous(key=key, rows=">1")
        row = rows[0]
        diff = {}
        got = {}
        for f in want:
            got[f] = row[f]
            if not row[f + "_matches"]:
                diff[f] = {"expected": want[f], "got": row[f]}

        if diff:
            return CheckResult.mismatch(key=key, diff=diff)
        return CheckResult.verified(key=key, row=got)

    return check
