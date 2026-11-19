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
