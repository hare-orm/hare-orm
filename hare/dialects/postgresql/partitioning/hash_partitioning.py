from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.constants import HASH_PARTITION_NAME_TEMPLATE
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.dialects.postgresql.partitioning.partitions.hash_partition import HashPartition
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
    locations: tuple[tuple[int, str | None, str | None], ...] = ()

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
        locations = tuple(
            sorted(
                ((remainder, schema, table) for remainder, schema, table in self.locations),
                key=lambda location: location[0],
            )
        )
        if len({item[0] for item in locations}) != len(locations) or any(
            remainder not in range(count) for remainder, _schema, _table in locations
        ):
            raise ConfigurationError("HashPartitioning has an invalid physical location for a remainder.")
        object.__setattr__(self, "locations", locations)

    def get_partition_names(self) -> list[str]:
        return [HASH_PARTITION_NAME_TEMPLATE.format(remainder=remainder) for remainder in range(self.partition_count)]

    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        return {name: partition.get_bound_sql(render_value) for name, partition in self.get_partitions().items()}

    def get_partitions(self) -> dict[str, HashPartition]:
        locations = {remainder: (schema, table) for remainder, schema, table in self.locations}
        partitions = {}
        for remainder, name in enumerate(self.get_partition_names()):
            schema, table = locations.get(remainder, (None, None))
            partitions[name] = HashPartition(name, self.partition_count, remainder, table=table, schema=schema)
        return partitions

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        return dataclasses.replace(
            self,
            locations=tuple(
                (partition.remainder, partition.schema, partition.table)
                for partition in partitions.values()
                if partition.table is not None or partition.schema is not None
            ),
        )
