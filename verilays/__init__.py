import functools
import inspect

from verilays import store

_dsn = None
registry = {}  # action type -> {"system": ..., "verify": ...}; the worker reads it after importing the app


def init(dsn):
    global _dsn
    store.create_table(dsn)
    _dsn = dsn


def action(type, system, *, verify):
    if not callable(verify):
        raise TypeError(f"verify must be a function, got {verify!r}")
    inspect.signature(verify).bind(None, None, None)  # the worker calls verify(params, result, row)

    def decorate(fn):
        sig = inspect.signature(fn)
        registry[type] = {"system": system, "verify": verify}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            result = fn(*args, **kwargs)
            store.insert_action(_dsn, type, system, dict(bound.arguments), result)
            return result

        return wrapper

    return decorate
