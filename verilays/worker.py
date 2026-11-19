import datetime
import logging

import psycopg
from psycopg.rows import dict_row

import verilays
from verilays.checks import CheckResult
from verilays.store import _jsonb

log = logging.getLogger("verilays")


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
            if entry is None:
                log.warning("action %s/%s skipped: type not registered",
                            row["action_id"], row["action_type"])
                continue

            attempt = row["attempts"] + 1
            try:
                res = entry["verify"](row["params"], row["result"], row)
                if not isinstance(res, CheckResult):
                    raise TypeError(f"check returned {res!r}, not a CheckResult")
            except Exception:
                log.warning("action %s/%s check raised, stays pending (attempt %d)",
                            row["action_id"], row["action_type"], attempt, exc_info=True)
                conn.execute(
                    "UPDATE verilays_actions SET attempts = %s, updated_at = %s WHERE action_id = %s",
                    (attempt, now, row["action_id"]),
                )
                continue

            # shortcut: mismatch/not_found/ambiguous stay pending; #4 and #5 add those transitions.
            status = "verified" if res.status == "verified" else "pending"
            conn.execute(
                "UPDATE verilays_actions SET status = %s, evidence = %s, attempts = %s,"
                " updated_at = %s WHERE action_id = %s",
                (status, _jsonb(res.evidence), attempt, now, row["action_id"]),
            )
            log.info("action %s/%s → %s (check %s, attempt %d) evidence=%s",
                     row["action_id"], row["action_type"], status, res.status, attempt,
                     res.evidence)
