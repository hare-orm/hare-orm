from __future__ import annotations

import dataclasses
from typing import ClassVar

from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.constants import HASH_PARTITION_COUNT_LIMIT, HASH_PARTITION_NAME_TEMPLATE
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class HashPartitioning(Partitioning):
    """Splits the rows evenly by a hash of the key - ``partition_count`` partitions,
    ``<table>_p0`` ... ``<table>_p<n-1>``, each ``FOR VALUES WITH (MODULUS n, REMAINDER i)``.
    Changing the count rebuilds the table.

    Attributes:
        fields: The fields of the partition key.
        partition_count: How many partitions - from 1 to ``HASH_PARTITION_COUNT_LIMIT``.
    """

    strategy: ClassVar[PartitionStrategy] = PartitionStrategy.HASH

    partition_count: int = 0

    def __post_init__(self) -> None:
        """Checks the key fields and the partition count.

        Raises:
            ConfigurationError: A key field is wrong, or the count isn't a whole number from 1 to
                ``HASH_PARTITION_COUNT_LIMIT``.
        """
        super().__post_init__()
        count = self.partition_count
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= HASH_PARTITION_COUNT_LIMIT:
            raise ConfigurationError(
                f"HashPartitioning: partition_count must be a whole number from 1 to {HASH_PARTITION_COUNT_LIMIT}, "
                f"got {count!r}"
            )

    def get_partition_names(self) -> list[str]:
        return [HASH_PARTITION_NAME_TEMPLATE.format(remainder=remainder) for remainder in range(self.partition_count)]

    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        return {
            name: f"FOR VALUES WITH (MODULUS {self.partition_count}, REMAINDER {remainder})"
            for remainder, name in enumerate(self.get_partition_names())
        }
