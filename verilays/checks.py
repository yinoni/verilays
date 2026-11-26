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
