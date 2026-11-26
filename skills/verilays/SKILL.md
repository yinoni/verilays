---
name: verilays
description: >-
  Set up Verilays on an AI agent, so every action the agent reports as done is checked against the system it wrote to. Use when the person wants to know whether their agent's tool calls really happened, says the agent claimed something that didn't land (a booking, an order, a refund, a cancellation), or asks to add Verilays, verify or check their agent's actions, or catch silent failures.
---

# Verilays

Verilays checks whether an agent's actions really happened:

- **A decorator on each tool** records what the tool claimed.
- **A worker** reads the record back from the system the tool wrote to, and marks the action `verified`, `mismatch` or `missing`.

Your job is to wire it into the person's agent and prove that one action comes back `verified`.

## Principles

1. **Read the README first.** Fetch https://github.com/yinoni/verilays#readme for the current install line and API. Don't write it from memory.
2. **Ask before you change their code.** Show the person the list of tools you plan to wrap and the check for each, and wait for their yes.
3. **Never trigger a real action to test it.** Call a tool only in a development or test environment, with test data, and only after the person says which one.
4. **Never ask for credentials in chat.** Database URLs go in environment variables or a `.env` file the person fills in.

## Workflow

### 1. Find the tools that change something

Read the agent's code and list its tools: functions with `@tool`, entries in a tools list, MCP tool handlers. For each one, note:

- whether it **changes something** (creates, updates, cancels or deletes a record, books, sends). Read-only tools, like search or lookup, need no check, so skip them.
- **where it writes:** which database and table, or which API.
- **what identifies the record:** the id it returns, or the id it was given.

The SDK is Python. If the tools are written in another language, tell the person, and point them to https://github.com/yinoni/verilays/issues to ask for their language.

### 2. Choose a check for each tool

- **It writes to Postgres** (including Supabase, Neon and RDS): read [references/postgres.md](references/postgres.md).
- **It writes anywhere else** (a calendar, a booking or CRM API, another database): read [references/other-systems.md](references/other-systems.md).

Show the person the plan before editing: each tool, where it writes, and the check you'll add.

### 3. Install and initialise

- Install Verilays as the README says, into the agent's environment and its dependency file.
- **The action store** is Verilays' own table, `verilays_actions`, in a Postgres they run. It can be the same Postgres as the app, or the `postgres` service from the repo's `docker-compose.yml`. Ask which.
- Call `verilays.init(os.environ["VERILAYS_DSN"])` once at the agent's startup, before any tool runs.

### 4. Wrap the tools

- Put the Verilays decorator directly on the function, under any framework decorator such as `@tool`.
- A LangGraph node takes the graph's state: use `verilays.langgraph.postgres(...)` or `verilays.langgraph(...)` in place of `verilays.action...`, with `fields_to_compare` naming keys of the state.
- Keep each tool's behaviour exactly as it was.

### 5. Run the worker

- **Environment variables:**
  - `VERILAYS_DSN`: the action store.
  - `VERILAYS_POSTGRES`: the database the Postgres checks read (`VERILAYS_POSTGRES_<DB>` for each tool with `db=`).
- **The command:** `verilays worker --every 10`, plus `--app <module>` when any tool uses a custom `verify=`.
- In production it runs as its own process or container. The repo's `Dockerfile` is a generic worker image configured with environment variables only.

### 6. Prove it works

1. In a development environment, call one wrapped tool once, with test data the person approves.
2. Run `verilays worker --once`, then `verilays report`.
3. The action should be `verified`.
   - **`check_error`:** the report says why, usually a missing environment variable.
   - **Stays `pending`:** the reason is in that action's `error` column in `verilays_actions`. It's usually a wrong table or column name, or a tool that doesn't return its id.
   
   Fix it and run again.

Don't call the setup done until one action shows `verified`.

### 7. Hand over

Tell the person:

- which tools are checked, and how;
- how to run the worker in production;
- that `verilays report` shows every action that needs attention, and why.

If a tool needed a custom check for a system others use too, mention that it could become a built-in check for everyone: `CONTRIBUTING.md` in the repo explains how, or they can open an issue. Only mention it once.
