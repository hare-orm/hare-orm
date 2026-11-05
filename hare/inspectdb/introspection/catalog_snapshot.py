from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Immutable raw records from one catalog read, before declaration inference.

    Section names belong to each dialect's reader. Every column returned by the
    database is retained, including metadata the mapper does not understand.
    Strings containing SQL are opaque. Arrays and mappings are frozen recursively;
    a mapper receives independent mutable records through ``copy_records()``.
    Taking a snapshot does not imply a transactionally consistent catalog: the
    caller chooses the connection and transaction enclosing the read.
    """

    schema: str
    tables: tuple[str, ...]
    records: Mapping[str, Sequence[Mapping[str, Any]]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "records", self.freeze(self.records))

    @classmethod
    def freeze(cls, value: Any) -> Any:
        """Freeze catalog containers without interpreting their scalar values."""
        if isinstance(value, Mapping):
            return MappingProxyType({key: cls.freeze(item) for key, item in value.items()})
        if isinstance(value, (tuple, list)):
            return tuple(cls.freeze(item) for item in value)
        return value

    @classmethod
    def copy_value(cls, value: Any) -> Any:
        """Give a declaration mapper private copies of nested catalog containers."""
        if isinstance(value, Mapping):
            return {key: cls.copy_value(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [cls.copy_value(item) for item in value]
        return value

    def copy_records(self) -> dict[str, list[dict[str, Any]]]:
        """Copy records for one independent mapping pass; no database work occurs."""
        return self.copy_value(self.records)
