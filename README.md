# Verilays

Catch agent actions that report success but didn't happen.

Agents say "done" when the action never landed, landed with wrong values, landed twice, or was undone later — silently. Verilays wraps an agent's tool, records what it claimed, and reads back from your own system on a schedule to report what actually happened.

Status: pre-v0. Design: `docs/features/v0-sdk/design.md`.
