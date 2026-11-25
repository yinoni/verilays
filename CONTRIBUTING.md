# Contributing to Verilays

Verilays is an early v0, built by two people. We want your help, and we read and answer every issue and pull request ourselves.

## What helps most

1. **A check for the system your agent writes to.** Verilays checks Postgres today. Booking systems are first on our list:
   - Google Calendar
   - Cal.com and Calendly
   - restaurant and hotel reservation APIs
   - and beyond bookings: MySQL, MongoDB, CRMs

   Look for issues labelled `good first issue`.
2. **A failure your agent hit.** Something Verilays missed, or caught. Open an issue with what the agent said and what actually happened. Real cases decide what we build next.
3. **Bugs.** What you ran, what you expected, and what happened.

Not sure it's wanted? Open an issue and ask first. Two lines in an issue can save you a pull request we can't merge.

## Run it locally

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker.

```
git clone https://github.com/yinoni/verilays
cd verilays
uv sync
docker compose up -d postgres     # the test database, on localhost:55432
uv run pytest
```

The demo runs with `docker compose run --build --rm demo`.

## Adding a check for a new system

**Start with a custom check.** Write it as a `verify=` function on your own agent, as shown in the README under "Not Postgres?". If it works there, it's already useful: open an issue with the code, even before it's built in.

**Then make it built in.** Follow how Postgres is done. A new system is one new file plus two small entries:

- `verilays/recipes/postgres.py`: what the decorator saves (`PostgresCheck`), and the function that looks the record up and returns a `CheckResult`. Your system gets its own file next to it.
- `verilays/__init__.py`: `action.postgres(...)`, the decorator people write on their tool.
- `verilays/worker.py`: one entry in `SYSTEMS`, which turns the saved check into a check function. Each system reads its connection from `VERILAYS_<SYSTEM>_<NAME>`.

Every check keeps four rules:

- **It only reads.** The worker never writes to the system it checks.
- **It never breaks the agent.** A wrong setting, or an action store that's down, leaves the tool running and returning as normal.
- **It saves only what it checks.** Arguments the check doesn't need, like a card number, are never stored.
- **It says what it found.** Return `verified`, `mismatch` (with what was expected and what was there), or `not_found`. The worker keeps checking `not_found` until the deadline, then marks it `missing`.

## Pull requests

- One change per pull request, kept small.
- Add a test for it, and make sure `uv run pytest` passes.
- In the description, say what it does, with a short example, and what happened before your change and after it. The merged pull requests show the shape.
- By contributing, you agree that your contribution is released under the project's license (see `LICENSE`).

## Questions

Open an issue. We answer every one.
