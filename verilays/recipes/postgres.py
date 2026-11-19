from psycopg import sql
from psycopg.rows import tuple_row

from verilays.checks import CheckResult


def pg_row_matches(connect, table, lookup_field, fields_to_compare):
    """Check that exactly one row in `table` matches `lookup_field` and has the `fields_to_compare` values.

    lookup_field: "id" finds the row where column id = result["id"].
    fields_to_compare: ["room"] checks that column room = params["room"].
    When names differ, pass a dict {column: name}; a value can also be fn(params, result).
    """
    if isinstance(lookup_field, str):
        lookup_field = {lookup_field: lookup_field}
    if not isinstance(fields_to_compare, dict):
        fields_to_compare = {f: f for f in fields_to_compare}

    def check(params, result, ctx):
        def from_result(name):
            if not isinstance(result, dict) or name not in result:
                raise LookupError(f"result has no field {name!r} (got {result!r})")
            return result[name]

        keys = {col: src(params, result) if callable(src) else from_result(src)
                for col, src in lookup_field.items()}
        wanted = {col: src(params, result) if callable(src) else params[src]
                  for col, src in fields_to_compare.items()}
        # Postgres does the comparing, so "10:00" matches a time column holding 10:00:00.
        query = sql.SQL("SELECT {cols} FROM {table} WHERE {where} LIMIT 2").format(
            cols=sql.SQL(", ").join(
                sql.SQL("{c}::text, {c} IS NOT DISTINCT FROM %s").format(c=sql.Identifier(c))
                for c in wanted
            ) if wanted else sql.SQL("1"),
            table=sql.Identifier(table),
            where=sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(c)) for c in keys
            ),
        )
        with connect() as conn:
            rows = conn.cursor(row_factory=tuple_row).execute(
                query, [*wanted.values(), *keys.values()]
            ).fetchall()

        if not rows:
            return CheckResult.not_found(key=keys)
        if len(rows) > 1:
            return CheckResult.ambiguous(key=keys, rows=">1")
        row = rows[0]
        got = {col: row[2 * i] for i, col in enumerate(wanted)}
        diff = {
            col: {"expected": wanted[col], "got": got[col]}
            for i, col in enumerate(wanted) if not row[2 * i + 1]
        }
        if diff:
            return CheckResult.mismatch(key=keys, diff=diff)
        return CheckResult.verified(key=keys, row=got)

    return check
