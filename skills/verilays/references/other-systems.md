---
description: A tool that writes to anything other than Postgres, such as a calendar, a booking or CRM API, or another database.
---

# A tool that writes anywhere else

Write a short check function that reads the record back through that system's own client, and pass it as `verify=`. It's the same question the Postgres check asks: is the record there, and does it hold what the agent said?

```python
from verilays.checks import CheckResult

def event_exists(params, result, row):
    event = calendar_client.get_event(result["event_id"])     # that system's own read call
    if event is None:
        return CheckResult.not_found()
    if event.start != params["start"]:
        return CheckResult.mismatch(diff={"start": {"expected": params["start"], "got": event.start}})
    return CheckResult.verified()

@verilays.action("create_event", system="calendar", verify=event_exists, deadline="2m")
def create_event(title, start): ...
```

The client calls above are placeholders. Fetch the system's current API docs, and use the read call the person's code already uses where one exists.

## How the check behaves

- **The arguments:** `params` holds the tool's arguments, `result` what it returned, and `row` the stored action.
- **Return** `verified()`, `mismatch(diff=...)` or `not_found()`.
  - The worker keeps checking `not_found` until the deadline, then marks it `missing`.
  - If the check raises, the worker retries it until the deadline, then marks it `expired`.
- **The check only reads.** Never create, change or delete anything inside it.
- **Credentials** come from environment variables, the same way the tool gets them.
- **The worker must import the function,** so put it in a module and run `verilays worker --app <module> --every 10`.

## Tell the person

- **A custom check stores the tool's arguments and its result in the action store,** because Verilays can't tell what the check reads. If a tool takes something sensitive, like a card number or a token, say so before wrapping it.
- **If the system is one many agents use** (Google Calendar, Cal.com, Calendly, a reservation API, MySQL), this check could become a built-in one, so nobody has to write it again. `CONTRIBUTING.md` explains how, or they can open an issue.
