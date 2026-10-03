from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.explicit_partitioning import ExplicitPartitioning
from hare.dialects.postgresql.partitioning.partition import Partition
from hare.dialects.postgresql.partitioning.range_partition import RangePartition
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class RangePartitioning(ExplicitPartitioning):
    """Splits the rows by ranges of the key - each ``RangePartition`` holds the rows from its
    lower bound up to (not including) its upper one, the default partition (``default_partition=``)
    every other row. The ranges don't overlap.

    Attributes:
        fields: The fields of the partition key.
        partitions: The ``RangePartition``s.
        default_partition: The name of the default partition, None for none.
    """

    strategy: ClassVar[PartitionStrategy] = PartitionStrategy.RANGE
    partition_class: ClassVar[type[Partition]] = RangePartition

    def raise_if_partitions_unsupported(self, model: type[Model], key_column_count: int) -> None:
        """Rejects partitions sharing a name, a bound without one value per key column and
        overlapping ranges.

        Args:
            model: The partitioned model.
            key_column_count: The number of key columns.

        Raises:
            ConfigurationError: The partitions don't fit.
        """
        super().raise_if_partitions_unsupported(model, key_column_count)
        for partition in self.partitions:
            for bound in (partition.from_values, partition.to_values):
                if len(bound) != key_column_count:
                    raise ConfigurationError(
                        f"{model.__name__}: RangePartition {partition.name!r} bound {bound!r} needs one value per "
                        f"key column ({key_column_count})"
                    )
        try:
            ordered = sorted(self.partitions, key=lambda partition: RangePartition.get_sort_key(partition.from_values))
        except TypeError as error:
            raise ConfigurationError(
                f"{model.__name__}: the RangePartition bounds of one key column must be values of one type - {error}"
            ) from None
        for previous, following in zip(ordered, ordered[1:], strict=False):
            if RangePartition.get_sort_key(previous.to_values) > RangePartition.get_sort_key(following.from_values):
                raise ConfigurationError(
                    f"{model.__name__}: the RangePartitions {previous.name!r} and {following.name!r} overlap"
                )
