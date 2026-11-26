"""Systems the worker checks an action against: one file per system.

A system file has:
  a dataclass      what its decorator saves in the action's `check` (PostgresCheck)
  verify_from_saved(saved_check, connections)
                   turns that saved check into verify(params, result, row) → CheckResult.
                   It reads its connection from VERILAYS_<SYSTEM> (VERILAYS_<SYSTEM>_<DB> when
                   the check names a db), opens it read-only once per pass, and keeps it in
                   `connections` (URL → connection, or None once it failed to answer in this pass).

Add it to SYSTEMS below.
"""
from verilays.systems import postgres

# The row's `system` column → that system's verify_from_saved.
SYSTEMS = {"postgres": postgres.verify_from_saved}
