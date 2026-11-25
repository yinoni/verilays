---
description: A tool that inserts, updates or deletes a row in Postgres (including Supabase, Neon and RDS).
---

# A tool that writes to Postgres

Use `@verilays.action.postgres(...)`. The worker finds the row and compares it with what the agent passed. No check code is needed.

```python
@verilays.action.postgres(tool_name="create_booking", db="bookings", table="bookings",
                          lookup_field="id", fields_to_compare=["room", "start_time"],
                          deadline="2m")
def create_booking(room, start_time):
    ...
    return {"id": new_id}
```

## Filling in each setting

- **`db`:** a short name you choose. The worker reads that database's URL from `VERILAYS_POSTGRES_<DB>`, upper-cased: `db="bookings"` → `VERILAYS_POSTGRES_BOOKINGS`.
- **`table`:** the table name as it appears in the database's default schema.
- **`lookup_field`:** the column that identifies the row, usually `id`. The tool must either return it in a dict (`{"id": 42}`) or take it as an argument (`cancel_booking(id)`), under the same name as the column.
- **`fields_to_compare`:** tool arguments that are saved as-is into columns of the same name. Leave out any argument whose column has a different name, or that the tool transforms before saving.
- **`expected`:** values the tool sets that aren't its arguments, such as `{"status": "cancelled"}` for a cancel tool.
- **`exists=False`:** for a delete tool. The action is verified once the row is gone.
- **`deadline`:** how long the write may take to appear: `"10s"`, `"5m"` or `"1h"` (default `"1m"`). Set it longer for tools that write through a queue or a slow API.
- **`duplicate_window`:** two calls with the same compared values inside this window flag the second as a duplicate (default `"1m"`).

## Gotchas

- A wrong setting doesn't break the tool. It leaves the tool unchecked and logs `<tool> runs but is not checked: <why>`. Look for that warning in the agent's logs after wiring.
- Only the lookup field and the compared fields are stored, so other arguments, like a card number, never reach the action store.
- Give the worker a read-only database user if you can. The worker also opens the connection read-only.
