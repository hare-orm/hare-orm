from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TestDatabaseIdentity:
    """A whole-database server resource, without credentials."""

    endpoint: tuple[str, int]
    database: str


@dataclass(frozen=True, slots=True)
class TestDatabaseLease:
    """An acquisition token; a stale token cannot release a later acquisition."""

    identity: TestDatabaseIdentity
    token: object = field(default_factory=object, repr=False)
    keep_schema: bool = False
