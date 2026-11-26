---
description: A tool that inserts, updates or deletes a row in Postgres (including Supabase, Neon and RDS).
---

# A tool that writes to Postgres

Use `@verilays.action.postgres(...)`. The worker finds the row and compares it with what the agent passed. No check code is needed.

```python
@verilays.action.postgres(table="bookings", lookup_field="id",
                          fields_to_compare=["room", "start_time"], deadline="2m")
def create_booking(room, start_time):
    ...
    return {"id": new_id}
```

## Filling in each setting

- **`db`:** leave it out when the agent writes to one database; the worker reads its URL from `VERILAYS_POSTGRES`. Only when it writes to several, give each a short name: `db="crm"` → `VERILAYS_POSTGRES_CRM`.
- **`table`:** the table name as it appears in the database's default schema.
- **`lookup_field`:** the column that identifies the row, usually `id`. The tool must either return it in a dict (`{"id": 42}`) or take it as an argument (`cancel_booking(id)`). When the name differs from the column, map it, column first: `lookup_field={"id": "booking_id"}`.
- **`fields_to_compare`:** tool arguments that are saved as-is into columns. A list when argument and column share a name (`["room"]`), a dict `{column: argument}` when they don't (`{"room_id": "room"}`). Leave out any argument the tool transforms before saving. Every argument named here must be in the tool's signature, or the tool is left unchecked with a warning.
- **`expected`:** values the tool sets that aren't its arguments, such as `{"status": "cancelled"}` for a cancel tool.
- **`exists=False`:** for a delete tool. The action is verified once the row is gone.
- **`deadline`:** how long the write may take to appear: `"10s"`, `"5m"` or `"1h"` (default `"1m"`). Set it longer for tools that write through a queue or a slow API.
- **`duplicate_window`:** two calls with the same compared values inside this window flag the second as a duplicate (default `"1m"`).

## Gotchas

- A wrong setting doesn't break the tool. It leaves the tool unchecked and logs `<tool> runs but is not checked: <why>`. Look for that warning in the agent's logs after wiring.
- Only the lookup field and the compared fields are stored, so other arguments, like a card number, never reach the action store.
- Give the worker a read-only database user if you can. The worker also opens the connection read-only.
