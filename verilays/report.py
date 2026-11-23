import psycopg
from psycopg.rows import dict_row

# Column order of the count table; a status not listed here goes at the end.
STATUS_ORDER = ["verified", "pending", "mismatch", "missing", "failed", "expired", "check_error",
                "ambiguous"]
NEEDS_ATTENTION = ["mismatch", "missing", "failed", "expired", "check_error", "ambiguous"]


def report(dsn):
    """A count table per tool and status, then every action that needs attention with a one-line reason."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        counts = conn.execute(
            "SELECT action_type, status, count(*) AS n FROM verilays_actions GROUP BY 1, 2"
        ).fetchall()
        if not counts:
            return "no actions recorded"
        # seconds_after: how long after the action it duplicates; NULL when it isn't a duplicate.
        attention = conn.execute(
            "SELECT a.*, extract(epoch FROM a.created_at - first.created_at) AS seconds_after"
            " FROM verilays_actions a"
            " LEFT JOIN verilays_actions first ON first.action_id = a.duplicate_of"
            " WHERE a.status = ANY(%s) OR a.duplicate_of IS NOT NULL"
            " ORDER BY a.created_at, a.action_id",
            (NEEDS_ATTENTION,),
        ).fetchall()

    lines = _count_table(counts)
    if attention:
        lines += ["", "Needs attention"]
        lines += [_attention_line(row) for row in attention]
    return "\n".join(lines)


def _count_table(counts):
    by_tool = {}
    for row in counts:
        by_tool.setdefault(row["action_type"], {})[row["status"]] = row["n"]
    seen = {row["status"] for row in counts}
    statuses = [s for s in STATUS_ORDER if s in seen] + sorted(seen - set(STATUS_ORDER))
    tool_width = max(len(tool) for tool in by_tool)

    lines = [" " * tool_width + "  " + "  ".join(statuses)]
    for tool in sorted(by_tool):
        cells = [str(by_tool[tool].get(s, 0)).rjust(len(s)) for s in statuses]
        lines.append(tool.ljust(tool_width) + "  " + "  ".join(cells))
    return lines


def _attention_line(row):
    status = row["status"]
    reasons = []
    if status == "mismatch" and "diff" in (row["evidence"] or {}):
        reasons += [f"{field}: expected {d['expected']}, got {d['got']}"
                    for field, d in row["evidence"]["diff"].items()]
    elif status == "missing":
        reasons.append(f"not found after {row['deadline_at'] - row['created_at']}")
    elif status == "expired":
        reasons.append(f"no check succeeded: {row['error']}")
    elif status in ("failed", "check_error"):
        reasons.append(row["error"])
    elif status in NEEDS_ATTENTION:
        reasons.append(str(row["evidence"]))  # a custom check's mismatch, or ambiguous
    if row["duplicate_of"] is not None:
        first_id = str(row["duplicate_of"])[:8]
        reasons.append(f"duplicate of {first_id}, {int(row['seconds_after'])}s later")

    label = status if status in NEEDS_ATTENTION else "duplicate"
    params = " ".join(f"{name}={value}" for name, value in row["params"].items())
    short_id = str(row["action_id"])[:8]
    return f"{short_id}  {label:<11}  {params}  {'; '.join(reasons)}"
