# Verilays

Catch agent actions that report success but didn't happen.

Agents say "done" when the action never landed, landed with wrong values, landed twice, or was undone later — silently. Verilays wraps an agent's tool, records what it claimed, and reads back from your own system on a schedule to report what actually happened.

Status: pre-v0. Design: `docs/features/v0-sdk/design.md`.

## Demo

Needs only Docker. From a clean checkout:

```
docker compose run --build --rm demo
```

It starts Postgres, recreates a `bookings` table (and clears `verilays_actions`), and runs a booking
agent through eight scenarios while the worker checks every 3s. After the 40s deadline it prints
`verilays report`. About a minute in all.

| scenario | what the tool does | outcome |
|---|---|---|
| happy path | writes the row | `verified` |
| silent no-op | returns `{"id": 999}`, writes nothing | `missing` at the deadline |
| wrong values | saves 11:00 instead of 10:00 | `mismatch`, with the diff |
| late write | returns the id now, writes the row 15s later | `verified` on a later check |
| duplicate | the same booking twice | both `verified`, the second listed as a duplicate |
| overwritten | writes the row; someone moves it to 12:00 after it was verified | `ambiguous` |
| tool raises | raises `RuntimeError` | `failed`; the agent gets the same exception |
| store down | works, but the action store can't be reached | not recorded; a warning is logged |

A silent no-op is `missing`, not `failed`: `failed` means the tool itself raised.
