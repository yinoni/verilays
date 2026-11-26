import dataclasses
import datetime
import functools
import inspect
import logging
import re
from collections.abc import Callable

from verilays import store
from verilays.recipes.postgres import PostgresCheck
from opentelemetry import trace

log = logging.getLogger(__name__)
_dsn = None

registry = {}

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


def parse_duration(text: str) -> datetime.timedelta:
    """"10s", "5m", "1h" → a timedelta. Anything else raises ValueError."""
    match = re.fullmatch(r"(\d+)([smh])", text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"deadline must look like '10s', '5m' or '1h', got {text!r}")
    amount, unit = match.groups()
    return datetime.timedelta(**{_UNITS[unit]: int(amount)})


def init(dsn: str) -> None:
    global _dsn
    store.create_table(dsn)
    _dsn = dsn

def _record(type, system, arguments, result, deadline, status="pending", error=None, check=None, run_id=None,
            span_id=None):
    if _dsn is None:
        log.warning("%s not recorded: verilays.init(dsn) was never called", type)
        return
    if arguments is None:  # _arguments already warned why
        return
    if check is not None:
        # action.postgres: save only what the worker checks, so other arguments (a card number,
        # a token) and the rest of the result never reach the store.
        [lookup] = check["lookup_field"].values()
        saved_names = {lookup, *check["fields_to_compare"].values()}
        arguments = {k: v for k, v in arguments.items() if k in saved_names}
        result = {lookup: result[lookup]} if isinstance(result, dict) and lookup in result else None
    try:
        store.insert_action(_dsn, type, system, arguments, result, deadline, status=status,
                            error=error, check=check, run_id=run_id, span_id=span_id)
    except Exception as e:
        log.warning("insert record error: %s", e)

def _current_trace_ids():
    """The caller's OTel/Langfuse (trace id, span id), or (None, None) when the tool runs outside a trace.

    The span id is the current span: with Langfuse's LangChain handler, the tool call itself.
    """
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    trace_id = format(ctx.trace_id, "032x")
    span_id = format(ctx.span_id, "016x")
    return trace_id, span_id

def _arguments(name, bound, from_state):
    """The arguments the check reads: the tool's own, or with from_state the fields of its first argument.

    A LangGraph node takes one state: a dict or TypedDict, a dataclass, or a Pydantic model.
    Any other state gives None, so the call isn't recorded, with a warning.
    """
    if not from_state:
        return dict(bound.arguments)
    state = next(iter(bound.arguments.values()), None)
    if isinstance(state, dict):
        return dict(state)
    if dataclasses.is_dataclass(state) and not isinstance(state, type):
        return {field.name: getattr(state, field.name) for field in dataclasses.fields(state)}
    if hasattr(state, "model_dump"):
        return state.model_dump()
    log.warning("%s runs but is not checked: its state must be a dict, dataclass or Pydantic model, got %r",
                name, state)
    return None

def _action(tool_name: str | None = None, *, system: str, verify: Callable | None = None, check: dict | None = None,
           deadline: str = "1m", from_state: bool = False) -> Callable:
    """With check (saved in the row), the worker needs no app code; without it, verify is required.

    tool_name: the function's name when left out; pass it when the agent knows the tool by another name.

    deadline: how long the worker keeps looking for the write before the action is missing.

    from_state: read the checked fields from the first argument (a LangGraph node's state).
    """
    deadline = parse_duration(deadline)
    if check is None:
        if not callable(verify):
            raise TypeError(f"verify must be a function, got {verify!r}")
        inspect.signature(verify).bind(None, None, None)  # the worker calls verify(params, result, row)

    def decorate(fn):
        name = tool_name or fn.__name__
        sig = inspect.signature(fn)
        if verify is not None:
            registry[name] = {"system": system, "verify": verify}

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                run_id, span_id = _current_trace_ids()
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                arguments = _arguments(name, bound, from_state)
                try:
                    result = await fn(*args, **kwargs)  # the only real difference: await
                except Exception as e:
                    _record(name, system, arguments, None, deadline, status="failed", error=repr(e),
                            check=check, run_id=run_id, span_id=span_id)
                    raise
                _record(name, system, arguments, result, deadline, check=check, run_id=run_id,
                        span_id=span_id)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                run_id, span_id = _current_trace_ids()
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                arguments = _arguments(name, bound, from_state)
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    _record(name, system, arguments, None, deadline, status="failed", error=repr(e),
                            check=check, run_id=run_id, span_id=span_id)
                    raise
                _record(name, system, arguments, result, deadline, check=check, run_id=run_id,
                        span_id=span_id)
                return result

        return wrapper

    return decorate


def _leave_unchecked(fn, tool_name, error):
    name = tool_name or fn.__name__
    log.warning("%s runs but is not checked: %s", name, error)
    return fn


def _postgres(tool_name: str | None = None, *, table: str, db: str | None = None, lookup_field: str | dict,
              fields_to_compare: list[str] | dict | None = None, deadline: str = "1m", duplicate_window: str = "1m",
              exists: bool = True, expected: dict | None = None, from_state: bool = False) -> Callable:
    """The worker reads the database's URL from VERILAYS_POSTGRES and checks the row with pg_row_matches.

    db: only when the agent writes to more than one database; db="crm" reads VERILAYS_POSTGRES_CRM instead.

    lookup_field: "id", or {"id": "booking_id"} when the tool returns (or takes) the id under another name.
    fields_to_compare: ["room"] when the argument and the column share a name,
    {"room_id": "room"} (column: argument) when they don't.
    duplicate_window: an earlier call with the same fields_to_compare values within this time
    marks the later one as its duplicate.
    exists=False: for a delete tool, verified once the row with lookup_field is gone.
    expected: {"status": "cancelled"} for values the tool sets that aren't its arguments;
    a field in both fields_to_compare and expected is checked against expected.
    """
    if fields_to_compare is None:
        fields_to_compare = []
    # Fail open: a mistake here must not break the tool, only leave it unchecked.
    try:
        parse_duration(deadline)
        parse_duration(duplicate_window)
        check = PostgresCheck(db=db, table=table, lookup_field=lookup_field, fields_to_compare=fields_to_compare,
                              duplicate_window=duplicate_window, exists=exists, expected=expected)
    except (TypeError, ValueError) as e:
        error = e  # `e` is cleared when the except block ends
        return lambda fn: _leave_unchecked(fn, tool_name, error)

    record = _action(tool_name, system="postgres", check=dataclasses.asdict(check), deadline=deadline,
                     from_state=from_state)

    def decorate(fn):
        if from_state:  # the state's fields only exist once the node is called
            return record(fn)
        # A typo or a renamed argument would otherwise surface later as a mismatch on every call.
        takes = inspect.signature(fn).parameters
        missing = [argument for argument in check.fields_to_compare.values() if argument not in takes]
        if missing:
            error = f"fields_to_compare names {missing}, which {fn.__name__} doesn't take"
            return _leave_unchecked(fn, tool_name, error)
        return record(fn)

    return decorate


class _Action:
    """verilays.action(...) with your own verify; verilays.action.postgres(...) for a Postgres row.

    A class, not a function with a .postgres attribute, so editors see both signatures.
    """
    __call__ = staticmethod(_action)
    postgres = staticmethod(_postgres)


action = _Action()


class _LangGraph:
    """verilays.langgraph(...) and verilays.langgraph.postgres(...), for a LangGraph node.

    The same as verilays.action, except the checked fields are read from the node's state, its first argument.
    A new system added to _Action gets its line here too.
    """
    __call__ = staticmethod(functools.partial(_action, from_state=True))
    postgres = staticmethod(functools.partial(_postgres, from_state=True))


langgraph = _LangGraph()
