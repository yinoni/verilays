from psycopg import sql
from psycopg.rows import tuple_row

from verilays.checks import CheckResult


def pg_row_matches(connect, table, key, expect):
    """Check that exactly one row in `table` matches `key` and has the `expect` values.

    Each value in `key` and `expect` is a param name (str) or fn(params, result).
    """

    def check(params, result, ctx):
        def value(src):
            return src(params, result) if callable(src) else params[src]

        keys = {col: value(src) for col, src in key.items()}
        wanted = {col: value(src) for col, src in expect.items()}
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
