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

def _record(type, system, arguments, result, status="pending", error=None):
    try:
        store.insert_action(_dsn, type, system, arguments, result, status=status, error=error)
    except Exception as e:
        log.warning("insert record error: %s", e)

def action(type, system, *, verify):
    if not callable(verify):
        raise TypeError(f"verify must be a function, got {verify!r}")
    inspect.signature(verify).bind(None, None, None)  # the worker calls verify(params, result, row)

    def decorate(fn):
        sig = inspect.signature(fn)
        registry[type] = {"system": system, "verify": verify}

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = await fn(*args, **kwargs)  # the only real difference: await
                except Exception as e:
                    _record(type, system, dict(bound.arguments), None, status="failed", error=repr(e))
                    raise
                _record(type, system, dict(bound.arguments), result)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    _record(type, system, dict(bound.arguments), None, status="failed", error=repr(e))
                    raise
                _record(type, system, dict(bound.arguments), result)
                return result

        return wrapper

    return decorate
