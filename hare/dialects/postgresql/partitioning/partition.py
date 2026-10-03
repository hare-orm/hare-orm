from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, ClassVar

from hare.dialects.enums import DialectName
from hare.utils.class_path import ClassPath


@dataclasses.dataclass(frozen=True)
class Partition:
    """One partition of a ``ListPartitioning``/``RangePartitioning`` - a table of its own,
    ``<table>_<name>``, which the migrations add (``AddPartition``) and remove (``RemovePartition``)
    one at a time.

    Attributes:
        name: The partition's name - letters, digits and underscores.
    """

    #: The dialect whose table options hold the partition.
    dialect_name: ClassVar[str] = DialectName.POSTGRESQL

    name: str

    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        """The partition's bound as ``CREATE TABLE ... PARTITION OF`` takes it.

        Args:
            render_value: Renders a bound value of the key column at a position.

        Returns:
            ``FOR VALUES ...`` or ``DEFAULT``.
        """
        raise NotImplementedError()  # pragma: nocoverage

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
        }
        return ClassPath.get(type(self)), [], kwargs
