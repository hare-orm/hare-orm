from __future__ import annotations

import dataclasses
from typing import ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.constants import HASH_PARTITION_NAME_TEMPLATE
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class HashPartitioning(Partitioning):
    """Splits the rows evenly by a hash of the key - ``partition_count`` partitions,
    ``<table>_p0`` ... ``<table>_p<n-1>``, each ``FOR VALUES WITH (MODULUS n, REMAINDER i)``.
    Changing the count rebuilds the table.

    Attributes:
        fields: The fields of the partition key.
        partition_count: A positive integer number of partitions.
    """

    strategy: ClassVar[PartitionStrategy] = PartitionStrategy.HASH

    partition_count: int = 0

    def __post_init__(self) -> None:
        """Checks the key fields and the partition count.

        Raises:
            ConfigurationError: A key field is wrong, or the count isn't a positive whole number.
        """
        super().__post_init__()
        count = self.partition_count
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ConfigurationError(
                "HashPartitioning: partition_count must be a whole number greater than zero, "
                f"got {ValueDescriptions.get_shown_value(count)}"
            )

    def get_partition_names(self) -> list[str]:
        return [HASH_PARTITION_NAME_TEMPLATE.format(remainder=remainder) for remainder in range(self.partition_count)]

    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        return {
            name: f"FOR VALUES WITH (MODULUS {self.partition_count}, REMAINDER {remainder})"
            for remainder, name in enumerate(self.get_partition_names())
        }
