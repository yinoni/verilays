from psycopg import sql
from psycopg.rows import dict_row

from verilays.checks import CheckResult


def pg_row_matches(connect, table, lookup_field, fields_to_compare):
    """Check that exactly one row in `table` matches `lookup_field` and has the `fields_to_compare` values.

    lookup_field: "id" finds the row where column id = result["id"].
    fields_to_compare: ["room"] checks that column room = params["room"].
    """
    def check(params, result, ctx):
        def from_result(name):
            if not isinstance(result, dict) or name not in result:
                raise LookupError(f"result has no field {name!r} (got {result!r})")
            return result[name]

        key = {lookup_field: from_result(lookup_field)}
        expected_values = [params[f] for f in fields_to_compare]
        # Postgres does the comparing, so "10:00" matches a time column holding 10:00:00.
        # Each field comes back twice: room (the DB value) and room_matches (True/False).
        query = sql.SQL("SELECT {cols} FROM {table} WHERE {where} LIMIT 2").format(
            cols=sql.SQL(", ").join(
                sql.SQL("{c}::text AS {c}, {c} IS NOT DISTINCT FROM %s AS {m}").format(
                    c=sql.Identifier(f), m=sql.Identifier(f + "_matches"))
                for f in fields_to_compare
            ) if fields_to_compare else sql.SQL("1"),
            table=sql.Identifier(table),
            where=sql.SQL("{} = %s").format(sql.Identifier(lookup_field)),
        )
        with connect() as conn:
            rows = conn.cursor(row_factory=dict_row).execute(
                query, [*expected_values, key[lookup_field]]
            ).fetchall()

        if not rows:
            return CheckResult.not_found(key=key)
        if len(rows) > 1:
            return CheckResult.ambiguous(key=key, rows=">1")
        row = rows[0]
        diff = {}
        got = {}
        for f in fields_to_compare:
            got[f] = row[f]
            if not row[f + "_matches"]:
                diff[f] = {"expected": params[f], "got": row[f]}

        if diff:
            return CheckResult.mismatch(key=key, diff=diff)
        return CheckResult.verified(key=key, row=got)

    return check
