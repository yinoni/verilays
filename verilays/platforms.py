"""Observability platforms the worker sends each verdict to.

A platform is a class with:
  name             shown in the worker's logs
  env              the env var that turns it on; the worker opens it only when this is set
  __init__()       reads its own config from env vars
  send(row, status, evidence)  sends one verdict; row is the verilays_actions row as it was before
                   this check (run_id, span_id, action_id, ...), evidence is what this check found
  flush()          called at the end of each pass, for clients that send in the background

Add it to PLATFORMS below.
"""


# The first line of a verdict's description: what the status means for the person reading the trace.
STATUS_SENTENCES = {
    "verified": "Verified: the record is in the system, as the tool reported",
    "mismatch": "Mismatch: the record in the system doesn't match what the tool was expected to do",
    "missing": "Missing: we didn't find the record in the system before the deadline",
    "ambiguous": "Ambiguous: the record matched at first, then changed after it was verified",
    "expired": "Expired: the check kept failing until the deadline, so we couldn't tell",
}


def describe(status, evidence):
    """The verdict as lines a person reads: the status's sentence, then the evidence, e.g.
    "Mismatch: ..." / "Looked up id=1" / "room: expected 4, got 9".
    Keys a recipe doesn't set (a custom check's evidence) are shown as "key: value".
    """
    lines = [STATUS_SENTENCES.get(status, status)]
    for name, value in (evidence or {}).items():
        # A custom check can use these names for anything, so only the recipe's shapes get a sentence.
        if name == "key" and isinstance(value, dict):
            lookup = ", ".join(f"{field}={field_value}" for field, field_value in value.items())
            lines.append(f"Looked up {lookup}")
        elif name == "diff" and isinstance(value, dict):
            for field, values in value.items():
                expected = values.get("expected")
                got = values.get("got")
                lines.append(f"{field}: expected {expected}, got {got}")
        elif name == "still_exists":
            lines.append("The row still exists, but the tool should have deleted it")
        elif name == "changed_after_verify":
            pass  # the ambiguous sentence already says it
        elif name == "rows" and value == ">1":
            lines.append("More than one row matches")
        else:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


class _Langfuse:
    """Sends each verdict as a score on the tool call's observation (trace id = run_id, observation
    id = span_id). Langfuse() reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL itself.
    """
    name = "langfuse"
    env = "LANGFUSE_SECRET_KEY"

    def __init__(self):
        from langfuse import Langfuse  # optional: pip install verilays[langfuse]
        self.client = Langfuse()

    def send(self, row, status, evidence):
        # score_id = action_id: a later verdict for the same action replaces the earlier one.
        action_id = str(row["action_id"])
        # On a mismatch this includes each field's expected and got: real row values.
        comment = describe(status, evidence)
        self.client.create_score(name="verilays", value=status, data_type="CATEGORICAL",
                                 trace_id=row["run_id"], observation_id=row["span_id"], score_id=action_id,
                                 comment=comment)

    def flush(self):
        self.client.flush()


# One entry per observability platform. A pass sends each verdict to every platform whose `env` is set.
PLATFORMS = [_Langfuse]
