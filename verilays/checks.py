from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckResult:
    status: str
    evidence: dict = field(default_factory=dict)

    @classmethod
    def verified(cls, **evidence):
        return cls("verified", evidence)

    @classmethod
    def mismatch(cls, **evidence):
        return cls("mismatch", evidence)

    @classmethod
    def not_found(cls, **evidence):
        return cls("not_found", evidence)

    @classmethod
    def ambiguous(cls, **evidence):
        return cls("ambiguous", evidence)


class ConfigError(Exception):
    """The worker is set up wrong (a missing env var, an unknown system); retrying won't help."""


class DatabaseDown(Exception):
    """A checked database already failed to answer in this pass; its rows wait for the next pass."""


def by_column(names):
    """"id" or ["room"] → {"id": "id"} or {"room": "room"}: the column and the argument share a name.
    A dict {column: argument} is already in this shape.
    """
    if isinstance(names, dict):
        return names
    if isinstance(names, str):
        return {names: names}
    return {name: name for name in names}


def lookup_value(params, result, name):
    if isinstance(result, dict) and name in result:
        return result[name]
    if name in params:
        return params[name]
    raise LookupError(f"neither result nor params has field {name!r} (result was {result!r})")


def check_with_finder(find, lookup_name, compare):
    def verify(params, result, row):
        record = find(lookup_value(params, result, lookup_name))
        if record is None:
            return CheckResult.not_found()
        if not isinstance(record, dict):
            raise TypeError(f"find must return a dict or None, got {type(record).__name__}")

        diff = {}
        for field, argument in compare.items():
            expected = params[argument]
            if field in record and record[field] == expected:
                continue
            diff[field] = {"expected": expected, "got": record.get(field)}
        if diff:
            return CheckResult.mismatch(diff=diff)
        return CheckResult("verified", {field: record[field] for field in compare})

    return verify
