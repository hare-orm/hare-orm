from __future__ import annotations

import abc
import dataclasses
from collections.abc import Callable
from typing import Any

from hare.classes.class_path import ClassPath
from hare.dialects.base.literals.constants import SQL_NULL_BYTE
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class Partition(abc.ABC):
    """One partition of a ``ListPartitioning``/``RangePartitioning`` - a table of its own,
    ``<table>_<name>``, which the migrations add (``AddPartition``) and remove (``RemovePartition``)
    one at a time.

    Attributes:
        name: The partition's name - letters, digits and underscores.
        table: An explicit physical table name. None derives it from the parent and partition names.
        schema: An explicit physical schema. None uses the parent table's schema.
    """

    name: str
    table: str | None = dataclasses.field(default=None, kw_only=True)
    schema: str | None = dataclasses.field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        Partitioning.check_partition_name(type(self).__name__, self.name)
        for identifier in (self.table, self.schema):
            if identifier is not None and (not identifier or SQL_NULL_BYTE in identifier):
                raise ConfigurationError(f"Partition {self.name!r} has an invalid physical identifier.")

    def get_location(self, parent_table: str, parent_schema: str | None) -> tuple[str | None, str]:
        """The schema and table migration statements address."""
        table = self.table or Partitioning.get_partition_table_name(parent_table, self.name)
        return self.schema or parent_schema, table

    def has_same_bound(self, other: object) -> bool:
        """Whether changing only the partition's name and address preserves its bound."""
        return (
            isinstance(other, Partition)
            and dataclasses.replace(self, name=other.name, table=other.table, schema=other.schema) == other
        )

    @abc.abstractmethod
    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        """The partition's bound as ``CREATE TABLE ... PARTITION OF`` takes it.

        Args:
            render_value: Renders a bound value of the key column at a position.

        Returns:
            ``FOR VALUES ...`` or ``DEFAULT``.
        """

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """How a migration file rebuilds the partition: its class path and arguments.

        Returns:
            The path, positional arguments and keyword arguments.
        """
        kwargs = {
            option.name: list(getattr(self, option.name))
            if isinstance(getattr(self, option.name), tuple)
            else getattr(self, option.name)
            for option in dataclasses.fields(self)
            if getattr(self, option.name) is not None or option.default is not None
        }
        return ClassPath.get(type(self)), [], kwargs
