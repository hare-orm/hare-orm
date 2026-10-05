from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.explicit_partitioning import ExplicitPartitioning
from hare.dialects.postgresql.partitioning.partitions.list_partition import ListPartition
from hare.dialects.postgresql.partitioning.partitions.partition import Partition
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class ListPartitioning(ExplicitPartitioning):
    """Splits the rows by the value of one key column - each ``ListPartition`` holds the rows of
    the values it lists, the default partition (``default_partition=``) every other row.

    Attributes:
        fields: The one field of the partition key.
        partitions: The ``ListPartition``s.
        default_partition: The name of the default partition, None for none.
    """

    strategy: ClassVar[PartitionStrategy] = PartitionStrategy.LIST
    partition_class: ClassVar[type[Partition]] = ListPartition

    def raise_if_partitions_unsupported(self, model: type[Model], key_column_count: int) -> None:
        """Rejects a key of several columns, partitions sharing a name and a value two partitions
        list.

        Args:
            model: The partitioned model.
            key_column_count: The number of key columns.

        Raises:
            ConfigurationError: The partitions don't fit.
        """
        super().raise_if_partitions_unsupported(model, key_column_count)
        if key_column_count != 1:
            raise ConfigurationError(
                f"{model.__name__}: ListPartitioning takes a key of one column, got {self.fields!r}"
            )
        seen: list[tuple[Any, str]] = []
        for partition in self.partitions:
            for value in partition.values:
                listed_by = next((name for seen_value, name in seen if seen_value == value), None)
                if listed_by is not None:
                    raise ConfigurationError(
                        f"{model.__name__}: the value {value!r} is listed by the partitions {listed_by!r} and "
                        f"{partition.name!r}"
                    )
                seen.append((value, partition.name))
