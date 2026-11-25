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

def _record(type, system, arguments, result, deadline, status="pending", error=None, check=None, run_id=None):
    if _dsn is None:
        log.warning("%s not recorded: verilays.init(dsn) was never called", type)
        return
    if check is not None:
        # action.postgres: save only what the worker checks, so other arguments (a card number,
        # a token) and the rest of the result never reach the store.
        lookup = check["lookup_field"]
        arguments = {k: v for k, v in arguments.items() if k == lookup or k in check["fields_to_compare"]}
        result = {lookup: result[lookup]} if isinstance(result, dict) and lookup in result else None
    try:
        store.insert_action(_dsn, type, system, arguments, result, deadline, status=status,
                            error=error, check=check, run_id=run_id)
    except Exception as e:
        log.warning("insert record error: %s", e)

def _current_trace_id():
    """The caller's OTel/Langfuse trace id, or None when the tool runs outside a trace."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None

def _action(tool_name: str, system: str, *, verify: Callable | None = None, check: dict | None = None,
           deadline: str = "1m") -> Callable:
    """With check (saved in the row), the worker needs no app code; without it, verify is required.

    deadline: how long the worker keeps looking for the write before the action is missing.
    """
    deadline = parse_duration(deadline)
    if check is None:
        if not callable(verify):
            raise TypeError(f"verify must be a function, got {verify!r}")
        inspect.signature(verify).bind(None, None, None)  # the worker calls verify(params, result, row)

    def decorate(fn):
        sig = inspect.signature(fn)
        if verify is not None:
            registry[tool_name] = {"system": system, "verify": verify}

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                run_id = _current_trace_id()
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = await fn(*args, **kwargs)  # the only real difference: await
                except Exception as e:
                    _record(tool_name, system, dict(bound.arguments), None, deadline, status="failed", error=repr(e),
                            check=check, run_id=run_id)
                    raise
                _record(tool_name, system, dict(bound.arguments), result, deadline, check=check, run_id=run_id)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                run_id = _current_trace_id()
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    _record(tool_name, system, dict(bound.arguments), None, deadline, status="failed", error=repr(e),
                            check=check, run_id=run_id)
                    raise
                _record(tool_name, system, dict(bound.arguments), result, deadline, check=check, run_id=run_id)
                return result

        return wrapper

    return decorate


def _postgres(tool_name: str, db: str, table: str, lookup_field: str, fields_to_compare: list[str] | None = None,
              deadline: str = "1m", duplicate_window: str = "1m", exists: bool = True,
              expected: dict | None = None) -> Callable:
    """The worker reads db's URL from VERILAYS_POSTGRES_<DB> and checks the row with pg_row_matches.

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
        log.warning("%s runs but is not checked: %s", tool_name, e)
        return lambda fn: fn
    return _action(tool_name, "postgres", check=dataclasses.asdict(check), deadline=deadline)


class _Action:
    """verilays.action(...) with your own verify; verilays.action.postgres(...) for a Postgres row.

    A class, not a function with a .postgres attribute, so editors see both signatures.
    """
    __call__ = staticmethod(_action)
    postgres = staticmethod(_postgres)


action = _Action()
