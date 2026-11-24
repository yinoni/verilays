# Verilays

An agent's "done" is a self-report. Verilays is an open-source tool that checks your database to see if what the agent claimed actually happened.

[Demo](#demo) · [Use it on your agent](#use-it-on-your-agent) · [How it works](#how-it-works) · [Statuses](#statuses)

Your agent tells a customer "you're booked for Tuesday at 10:00". The tool returned success, the trace looks fine, and nothing reached the database. Or it landed at 11:00. Or it landed twice. Verilays records what each tool claimed, reads the record back from your own Postgres, and tells you which of these happened.

```
                verified  mismatch  missing  failed  ambiguous
create_booking         4         1        1       1          1

Needs attention
e54edf4d  missing      room=noop start_time=10:00  not found after 0:00:40
75a021a2  mismatch     room=wrong start_time=10:00  start_time: expected 10:00, got 11:00:00
6b909e47  duplicate    room=dup start_time=10:00  duplicate of 1a88226b, 0s later
496a4779  ambiguous    room=overwritten start_time=10:00  changed after verified: start_time 10:00 → 12:00:00
970b8ac4  failed       room=raises start_time=10:00  RuntimeError('booking API down')
```

That's real output from the demo below.

## Demo

Needs only Docker. From a clean checkout:

```
docker compose run --build --rm demo
```

It starts Postgres and runs a booking agent through eight scenarios while the worker checks every 3 seconds. After the 40-second deadline it prints `verilays report`. About a minute in all.

| scenario | what the tool does | outcome |
|---|---|---|
| happy path | writes the row | `verified` |
| silent no-op | returns `{"id": 999}`, writes nothing | `missing` at the deadline |
| wrong values | saves 11:00 instead of 10:00 | `mismatch`, with the diff |
| late write | returns the id now, writes the row 15s later | `verified` on a later check |
| duplicate | the same booking twice | the second is listed as a duplicate |
| overwritten | writes the row; someone moves it to 12:00 after it was verified | `ambiguous` |
| tool raises | raises `RuntimeError` | `failed`; the agent gets the same exception |
| store down | works, but the action store can't be reached | not recorded; a warning is logged |

## Use it on your agent

**1. Install it**, and start the action store (Verilays' own table, in a Postgres you run):

```
pip install git+https://github.com/yinoni/verilays
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

The tool must return a dict holding `lookup_field` (`{"id": 42}`), or take it as an argument (`cancel_order(id)`) when it doesn't return it. Each name in `fields_to_compare` must be both a tool argument and a column. The worker then finds the row `orders.id = 42` and checks that its `customer` and `amount` match what the agent passed.

For a delete tool, pass `exists=False`: the action is verified once the row is gone, and a row still there at the deadline is a mismatch.

For a value the tool sets that isn't one of its arguments, pass `expected={"status": "cancelled"}`. A field in both `fields_to_compare` and `expected` is checked against `expected`.

A wrong setting, like `lookup_field=5` or `deadline="5 min"`, never breaks your tool: it still runs, isn't checked, and a warning says why (`create_order runs but is not checked: lookup_field must be str, got 5`).

**3. Run the worker.** `db="shop"` means it reads that database's URL from `VERILAYS_POSTGRES_SHOP`:

```
export VERILAYS_DSN=postgresql://verilays:verilays@localhost:55432/verilays
VERILAYS_POSTGRES_SHOP=postgresql://user:pass@localhost:5432/shop verilays worker --every 10
```

**4. Run your agent, then read the report:**

```
verilays report
```

## How it works

1. The decorator runs your tool unchanged, then records the tool name, its arguments, its result and a deadline in the action store.
2. The worker picks up pending actions and looks the record up in your database. It only reads there, so you can give it a read-only user.
3. It keeps checking until the deadline. Found with the right values is `verified`, different values is `mismatch`, and still not there at the deadline is `missing`.

If Verilays can't record a call, your tool still runs and returns as normal. It never breaks your agent.

Everything runs in your infrastructure: the action store is your Postgres, and the worker is your container (`Dockerfile`, configured with env vars only).

## Statuses

| status | meaning |
|---|---|
| `pending` | not found yet; checked again until the deadline |
| `verified` | the write is there, with the right values |
| `mismatch` | the write is there, with different values (the report shows the diff) |
| `missing` | still not found at the deadline |
| `ambiguous` | verified, then changed or deleted before the deadline |
| `failed` | the tool raised; the agent got the same exception |
| `expired` | the check itself kept failing until the deadline, so we never found out |
| `check_error` | the worker is set up wrong (e.g. a missing `VERILAYS_POSTGRES_*`); retried when it restarts |

An action with the same `fields_to_compare` values as one inside `duplicate_window` (default `1m`) is listed as a duplicate of it.

## What it doesn't do (yet)

- **Postgres only.** For anything else, write the check yourself (below).
- **It only sees actions that go through a wrapped tool.** If the agent never calls the tool, there's nothing to check.
- **It checks that the record matches what the agent passed, not whether it was the right action.**
- **A row that already existed with the same values reads as `verified`.**
- **The tool has to return the lookup key** (`{"id": 42}`).

### Not Postgres?

Write the check yourself and pass it as `verify=`. It gets the tool's arguments, its result and the action's row, and returns a `CheckResult`:

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

## Feedback

Which system should Verilays check next? Something it missed, or caught? [Open an issue](../../issues). We read every one.
