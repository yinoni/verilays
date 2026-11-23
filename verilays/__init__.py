import datetime
import functools
import inspect
import logging
import re

from verilays import store

log = logging.getLogger(__name__)
_dsn = None

registry = {}

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


def parse_duration(text):
    """"10s", "5m", "1h" → a timedelta. Anything else raises ValueError."""
    match = re.fullmatch(r"(\d+)([smh])", text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"deadline must look like '10s', '5m' or '1h', got {text!r}")
    amount, unit = match.groups()
    return datetime.timedelta(**{_UNITS[unit]: int(amount)})


def init(dsn):
    global _dsn
    store.create_table(dsn)
    _dsn = dsn

def _record(type, system, arguments, result, deadline, status="pending", error=None, check=None):
    if _dsn is None:
        log.warning("%s not recorded: verilays.init(dsn) was never called", type)
        return
    try:
        store.insert_action(_dsn, type, system, arguments, result, deadline, status=status,
                            error=error, check=check)
    except Exception as e:
        log.warning("insert record error: %s", e)

def action(tool_name, system, *, verify=None, check=None, deadline="1m"):
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
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = await fn(*args, **kwargs)  # the only real difference: await
                except Exception as e:
                    _record(tool_name, system, dict(bound.arguments), None, deadline, status="failed", error=repr(e),
                            check=check)
                    raise
                _record(tool_name, system, dict(bound.arguments), result, deadline, check=check)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    _record(tool_name, system, dict(bound.arguments), None, deadline, status="failed", error=repr(e),
                            check=check)
                    raise
                _record(tool_name, system, dict(bound.arguments), result, deadline, check=check)
                return result

        return wrapper

    return decorate


def _postgres(tool_name, db, table, lookup_field, fields_to_compare, deadline="1m", duplicate_window="1m"):
    """The worker reads db's URL from VERILAYS_POSTGRES_<DB> and checks the row with pg_row_matches.

    duplicate_window: an earlier call with the same fields_to_compare values within this time
    marks the later one as its duplicate.
    """
    parse_duration(duplicate_window)  # a bad value fails here, not in the worker
    return action(tool_name, "postgres", check={"db": db, "table": table, "lookup_field": lookup_field,
                                           "fields_to_compare": fields_to_compare,
                                           "duplicate_window": duplicate_window}, deadline=deadline)


action.postgres = _postgres
