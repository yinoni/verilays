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

## Use it on your agent

**1. Install** it in your agent's project, and start the action store (Verilays' own table):

```
uv add --editable /path/to/verilays
docker compose up -d postgres        # in this repo; the store is on localhost:55432
```

**2. Wrap the tool.** For a tool that inserts into a Postgres table:

```python
import verilays

verilays.init("postgresql://verilays:verilays@localhost:55432/verilays")

@verilays.action.postgres(tool_name="create_order", db="shop", table="orders",
                          lookup_field="id", fields_to_compare=["customer", "amount"],
                          deadline="2m")
def create_order(customer, amount):
    ...
    return {"id": new_id}
```

The tool must return a dict holding `lookup_field` (`{"id": 42}`), and each name in
`fields_to_compare` must be both a tool argument and a column. The worker then finds the row
`orders.id = 42` and checks that its `customer` and `amount` match what the agent passed.

**3. Run the worker.** `db="shop"` means it reads that database's URL from `VERILAYS_POSTGRES_SHOP`:

```
export VERILAYS_DSN=postgresql://verilays:verilays@localhost:55432/verilays
VERILAYS_POSTGRES_SHOP=postgresql://user:pass@localhost:5432/shop uv run verilays worker --every 10
```

**4. Run your agent, then read the report:**

```
uv run verilays report
```

### Not Postgres?

Write the check yourself and pass it as `verify=`. It gets the tool's arguments, its result, and the
action's row, and returns a `CheckResult`:

```python
from verilays.checks import CheckResult

def order_exists(params, result, row):
    order = shop_api.get_order(result["id"])
    if order is None:
        return CheckResult.not_found()
    if order["amount"] != params["amount"]:
        return CheckResult.mismatch(diff={"amount": {"expected": params["amount"], "got": order["amount"]}})
    return CheckResult.verified()

@verilays.action("create_order", system="shop-api", verify=order_exists, deadline="2m")
def create_order(customer, amount): ...
```

The worker has to import that function, so point it at your module: `verilays worker --app myagent.tools --every 10`.

## Statuses

| status | meaning |
|---|---|
| `pending` | not found yet; checked again until the deadline |
| `verified` | the write is there, with the right values |
| `mismatch` | the write is there, with different values (the report shows the diff) |
| `missing` | still not found at the deadline |
| `ambiguous` | verified, then changed or deleted by someone else before the deadline |
| `failed` | the tool raised; the agent got the same exception |
| `expired` | the check itself kept failing until the deadline, so we never found out |
| `check_error` | the worker is set up wrong (e.g. a missing `VERILAYS_POSTGRES_*`); retried when it restarts |
