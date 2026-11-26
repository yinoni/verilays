# Verilays v0 — design

Agreed 2026-11-19 in a grilling session. No code before this doc; this doc is the spec for v0.

## Problem

Agents report success that didn't happen: the action never landed, landed with wrong values, landed twice, or was undone later. No error is raised. Traces show what the agent *did*; nothing shows what *happened* in the system of record.

## What v0 is

A Python SDK that wraps an agent's tool, records what the agent claimed, and later reads back from the customer's own system to classify the outcome. Deterministic only. Detect and report — no retries, no repairs.

The developer supplies the check (usually a recipe, sometimes a function). Verilays supplies everything around it: durable record of intent, scheduled rechecks, outcome classification, cross-run duplicate detection, and a report. (Like pytest: you write the assert, pytest runs and reports it.)

## The loop

1. `@verilays.action(...)` wraps the tool. Args are bound with `inspect.signature(...).bind` + `apply_defaults` → params.
2. The tool runs unchanged: its result is returned as-is, its exception is re-raised as-is.
3. The wrapper inserts one row (intent + result or error). That is all it does — no reads, no checks.
4. The worker does every check, starting with `recheck[0]`, then the rest of the schedule, then a final check at the deadline.
5. `verilays report` prints counts and the rows needing attention.

## API

```python
import verilays
from verilays.systems.postgres import pg_row_matches

verilays.init(dsn="postgres://...")          # where Verilays keeps its own table

@verilays.action(
    type="create_booking",
    system="postgres:bookings",              # label only
    entity_key=lambda p, r: f"booking:{p['book_id']}",
    verify=pg_row_matches(
        connect=get_bookings_conn,           # customer's own connection factory, per action
        table="bookings",
        lookup_field="id",                   # find the row where column id = result["id"]
        fields_to_compare=["start_time"],    # column start_time must = params["start_time"]
    ),
    recheck=["0s", "1m", "10m", "1h"],       # default
    deadline="24h",                          # default
    duplicate_window="24h",                  # default = deadline
)
def create_booking(book_id, start_time): ...
```

- `verify=` is **required**. A tool worth wrapping is worth checking.
- Recipe first: v0 ships one recipe, `pg_row_matches`. Custom checks are the fallback: `verify(params, result, ctx) -> CheckResult.verified | mismatch | not_found | ambiguous` (with evidence).
- Sync and async tools.
- Durations: `Ns | Nm | Nh`.

CLI:
```
verilays worker --app myapp.tools --once
verilays worker --app myapp.tools --loop 5s
verilays report
```
`--app` imports the developer's module; the `@action` decorators register themselves by `type`. The worker checks every registered type. A row whose type isn't registered is logged, skipped, and ends `expired`.

## Statuses

| status | meaning |
|---|---|
| `pending` | not decided yet |
| `verified` | read-back matches |
| `mismatch` | record found, values differ (diff in evidence) |
| `ambiguous` | was verified, later changed or disappeared (`changed_after_verify`) |
| `failed` | the tool raised, **or** we looked through the deadline and the record never appeared |
| `expired` | deadline passed and we never got a working check (check kept erroring / timing out / type not registered) |

Rules:
- A timeout or check error is "unknown", never `failed`.
- `not_found` stays `pending` until the deadline, then `failed`.
- Every action runs its full schedule; `verified` keeps being rechecked so overwrites are caught.
- `verified → mismatch | not_found` ⇒ `ambiguous` (`changed_after_verify`).
- Otherwise the latest conclusive check wins (`mismatch` at 10m, correct at 1h ⇒ `verified`).
- At the deadline the status freezes.

## Duplicates

Run B is a duplicate of run A when: same `action_type` + `entity_key`, A's `created_at` within B's `duplicate_window`, neither `failed`. Detected by the worker on B's first check. Marked with `duplicate_of = A.action_id`; B's status is unchanged (it did land — it landed twice).

## Data model

One table, `verilays_actions`, created by `verilays.init()` with `CREATE TABLE IF NOT EXISTS`. Lives in any Postgres the customer points `dsn` at (need not be their app DB).

| column | type |
|---|---|
| `action_id` | uuid pk |
| `run_id` | text null — Langfuse/OTel trace id later, no migration |
| `action_type` | text |
| `system` | text (label) |
| `entity_key` | text null |
| `params` | jsonb |
| `result` | jsonb null |
| `error` | text null |
| `status` | text |
| `evidence` | jsonb null |
| `duplicate_of` | uuid null |
| `attempts` | int |
| `next_check_at` | timestamptz null |
| `deadline_at` | timestamptz |
| `created_at`, `updated_at` | timestamptz |

Workers claim due rows with `FOR UPDATE SKIP LOCKED`, so several can run safely; one is the normal setup.

## Invariants

- Verilays breaking (store down, check bug) never breaks the tool: catch, log a warning, continue.
- No retries, no repairs.
- Nothing leaves the customer's environment in v0. Store, worker, and report are all local. Before anything ever leaves: default to status + ids only, and add redaction.
- Logging: log each decision and the number behind it.

## Report

Plain text to stdout:

```
type             pending verified mismatch ambiguous failed expired
create_booking         1       12        1         1      2       0

Needs attention:
a3f… booking:42  mismatch   start_time: expected 10:00, found 11:00
b71… booking:57  failed     not found after 24h
c09… booking:42  duplicate  same as a3f… (run 8 minutes earlier)
d22… booking:13  ambiguous  changed after verified: start_time 10:00 → 12:00
```

## Definition of done

pytest + Postgres via docker compose, one-command demo on a `bookings` table. Demo uses second-scale timings (e.g. late write at 30s, deadline 2m).

| scenario | injected | expected |
|---|---|---|
| happy path | row written | verified |
| silent no-op | success, nothing written | pending → failed at deadline |
| wrong values | different start_time | mismatch + diff evidence |
| late write | row appears 30s later | pending → verified on recheck |
| duplicate | two runs, same book_id | both verified, second has `duplicate_of` |
| overwritten | correct, then changed | verified → ambiguous (changed_after_verify) |
| tool raises | exception | failed, exception re-raised unchanged |
| store down | action store unavailable | tool still works, warning logged |

## Layout and stack

`verilays/{__init__,store,checks,scheduler,worker,report}.py`, `verilays/systems/postgres.py`, `examples/bookings_demo/`, `tests/`, `pyproject.toml`. Python 3.11+, psycopg 3, click. Any other dependency needs approval.

## Cut from v0

- Langfuse / OTel trace join (column reserved).
- LLM said-vs-did checks.
- Redaction hook (needed before anything leaves the env).
- SQLite store.
- `unchecked` status / coverage report (verify is required).
- Recipes other than `pg_row_matches` (Google Calendar is the likely next one).
- Migrations tooling.

## Decided outside the code

- Repo: this one, private. No push until the user says so.
- License: Apache 2.0 (team's final call). Reserve the `verilays` GitHub org name before going public.
