import functools
import inspect
import logging

from verilays import store

log = logging.getLogger(__name__)
_dsn = None

registry = {}


def init(dsn):
    global _dsn
    store.create_table(dsn)
    _dsn = dsn

def _record(type, system, arguments, result, status="pending", error=None, check=None):
    try:
        store.insert_action(_dsn, type, system, arguments, result, status=status, error=error,
                            check=check)
    except Exception as e:
        log.warning("insert record error: %s", e)

def action(type, system, *, verify=None, check=None):
    """With check (saved in the row), the worker needs no app code; without it, verify is required."""
    if check is None:
        if not callable(verify):
            raise TypeError(f"verify must be a function, got {verify!r}")
        inspect.signature(verify).bind(None, None, None)  # the worker calls verify(params, result, row)

    def decorate(fn):
        sig = inspect.signature(fn)
        if verify is not None:
            registry[type] = {"system": system, "verify": verify}

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = await fn(*args, **kwargs)  # the only real difference: await
                except Exception as e:
                    _record(type, system, dict(bound.arguments), None, status="failed", error=repr(e),
                            check=check)
                    raise
                _record(type, system, dict(bound.arguments), result, check=check)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    _record(type, system, dict(bound.arguments), None, status="failed", error=repr(e),
                            check=check)
                    raise
                _record(type, system, dict(bound.arguments), result, check=check)
                return result

        return wrapper

    return decorate


def _postgres(type, db, table, lookup_field, fields_to_compare):
    """The worker reads db's DSN from VERILAYS_DB_<DB> and checks the row with pg_row_matches."""
    return action(type, "postgres", check={"db": db, "table": table, "lookup_field": lookup_field,
                                           "fields_to_compare": fields_to_compare})


action.postgres = _postgres
