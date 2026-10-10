from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.partitioning.partitions.partition import Partition


@dataclasses.dataclass(frozen=True)
class DefaultPartition(Partition):
    """The default partition of a ``ListPartitioning``/``RangePartitioning`` - the rows no other
    partition holds. Declared as ``default_partition="<name>"``; the migrations add and remove it
    as this object.

    Attributes:
        name: The partition's name - its table is ``<table>_<name>``.
    """

    def __post_init__(self) -> None:
        """Checks the name.

        Raises:
            ConfigurationError: The name isn't letters, digits and underscores.
        """
        Partitioning.check_partition_name(type(self).__name__, self.name)

    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        return "DEFAULT"
