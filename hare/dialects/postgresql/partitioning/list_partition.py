from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.dialects.postgresql.partitioning.partition import Partition
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class ListPartition(Partition):
    """A partition of a ``ListPartitioning``: the rows whose key value is one of ``values``.

    Attributes:
        name: The partition's name - its table is ``<table>_<name>``.
        values: The key values the partition holds - None for the rows with a NULL key.
    """

    values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        """Freezes the values as a tuple and checks the partition.

        Raises:
            ConfigurationError: The name isn't letters, digits and underscores, or no value is given.
        """
        Partitioning.check_partition_name(type(self).__name__, self.name)
        values = tuple(self.values) if isinstance(self.values, (list, tuple, set, frozenset)) else (self.values,)
        if not values:
            raise ConfigurationError(f"ListPartition {self.name!r} needs at least one value")
        object.__setattr__(self, "values", values)

    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        return f"FOR VALUES IN ({', '.join(render_value(0, value) for value in self.values)})"
