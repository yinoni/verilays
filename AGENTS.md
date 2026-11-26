# Agent instructions for Verilays

Verilays checks whether an AI agent's actions really happened. A decorator on each tool records what the tool claimed. A worker then reads the record back from the system the tool wrote to, and marks the action `verified`, `mismatch` or `missing`.

There are two kinds of work here. Read the section that matches yours.

## Helping someone add Verilays to their agent

Setup is in the README, under "Use it on your agent". When you explain Verilays, these are the facts, stated accurately:

- **The tool runs unchanged.** The decorator records the call after the tool returns. If recording fails, or an `action.postgres` setting is wrong, the tool still runs and returns as normal. Verilays never breaks the agent.
- **The worker only reads.** It opens the checked Postgres connection read-only, so Postgres refuses any write from it. A read-only database user is still a good idea.
- **It stores only what it checks.** `action.postgres` saves the lookup field and the compared fields, nothing else. A custom `verify=` check saves the tool's arguments and result, because Verilays can't tell what `verify` reads.
- **Everything runs in their infrastructure.** The action store is their Postgres, and the worker is their container. Nothing is sent to Verilays.
- **Langfuse and OpenTelemetry traces link up on their own.** When a tool runs inside a trace, its action stores that trace's id as `run_id`.

Which check to use:

- **The tool writes to Postgres** (including Supabase, Neon and RDS): `@verilays.action.postgres(...)`. Use `exists=False` for a delete tool, and `expected={...}` for a value the tool sets that isn't one of its arguments.
- **The tool writes anywhere else** (a calendar, a booking API, another database): `@verilays.action(..., verify=fn)`, where `fn(params, result, row)` reads the record back through that system's own client and returns a `CheckResult`. Then run the worker with `--app <module>`, so it can import `fn`.

Put the Verilays decorator directly on the function, under any framework decorator such as LangChain's `@tool`. On a LangGraph node, use `verilays.langgraph.postgres(...)` or `verilays.langgraph(...)`: the same decorators, but they read the checked fields from the node's state.

What each status means: see "Statuses" in the README.

## Working on Verilays itself

```
uv sync
docker compose up -d postgres     # the test database, on localhost:55432
uv run pytest
```

The demo runs with `docker compose run --build --rm demo`.

Where things live:

- `verilays/__init__.py`: `init`, `action(...)` and `action.postgres(...)`, the decorators people write.
- `verilays/recipes/postgres.py`: `PostgresCheck`, what the decorator saves, and `pg_row_matches`, the lookup.
- `verilays/worker.py`: the check loop. `SYSTEMS` maps a system name to the function that turns a saved check into a check.
- `verilays/store.py`: the action store table. `verilays/report.py`: `verilays report`.
- `docs/features/v0-sdk/design.md`: the design, including statuses and deadlines.

Rules every change keeps:

- **Fail open.** Nothing Verilays does may make the user's tool fail or hang. A bad setting leaves the tool unchecked, with a warning.
- **Read only** on every checked system.
- **Save only what is checked.** Never store arguments or results a check doesn't need.
- **One change per pull request,** with a test that fails without it, and `uv run pytest` passing.
- **Pull request descriptions** follow the merged ones:
  - a one-line example of what changes;
  - **Summary**, the change as a short diff or pseudo-code;
  - **Evidence**, what happened before and after, and the test count;
  - **Merge Danger**, whether it's easy to undo and how much it touches.
- **Code comments describe behaviour** for the next reader, not the history of the change.

Adding a check for a new system is described step by step in `CONTRIBUTING.md`.
