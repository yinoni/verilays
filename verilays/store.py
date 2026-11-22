import datetime
import json
from functools import partial

import psycopg
from psycopg.types.json import Jsonb

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS verilays_actions (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id text,
    action_type text NOT NULL,
    system text NOT NULL,
    entity_key text,
    params jsonb NOT NULL,
    result jsonb,
    error text,
    status text NOT NULL,
    evidence jsonb,
    "check" jsonb,
    duplicate_of uuid,
    attempts int NOT NULL DEFAULT 0,
    next_check_at timestamptz,
    deadline_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

# Non-JSON values (dates, Decimals, ...) are stored as their str().
_jsonb = partial(Jsonb, dumps=partial(json.dumps, default=str))


def create_table(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute(CREATE_TABLE)


def insert_action(dsn, action_type, system, params, result, deadline=datetime.timedelta(minutes=1),
                  status="pending", error=None, check=None):
    # shortcut: one connection per call; add a pool if tool-call latency shows it.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """INSERT INTO verilays_actions
               (action_type, system, params, result, status, next_check_at, deadline_at, error, "check")
               VALUES (%s, %s, %s, %s, %s, now(), now() + %s, %s, %s)""",
            (action_type, system, _jsonb(params), _jsonb(result), status, deadline, error,
             _jsonb(check) if check else None),
        )
