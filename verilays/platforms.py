"""Observability platforms the worker sends each verdict to.

A platform is a class with:
  env              the env var that turns it on; the worker opens it only when this is set
  __init__()       reads its own config from env vars
  send(row, status)  sends one verdict; row is the verilays_actions row (run_id, span_id, action_id, ...)
  flush()          called at the end of each pass, for clients that send in the background

Add it to PLATFORMS below.
"""




class _Langfuse:
    """Sends each verdict as a score on the tool call's observation (trace id = run_id, observation
    id = span_id). Langfuse() reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL itself.
    """
    env = "LANGFUSE_SECRET_KEY"

    def __init__(self):
        from langfuse import Langfuse  # optional: pip install verilays[langfuse]
        self.client = Langfuse()

    def send(self, row, status):
        # score_id = action_id: a later verdict for the same action replaces the earlier one.
        action_id = str(row["action_id"])
        self.client.create_score(name="verilays", value=status, data_type="CATEGORICAL",
                                 trace_id=row["run_id"], observation_id=row["span_id"], score_id=action_id)

    def flush(self):
        self.client.flush()


# One entry per observability platform. A pass sends each verdict to every platform whose `env` is set.
PLATFORMS = {"langfuse": _Langfuse}
