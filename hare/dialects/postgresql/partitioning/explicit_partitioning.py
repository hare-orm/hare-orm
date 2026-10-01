from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.dialects.postgresql.partitioning.default_partition import DefaultPartition
from hare.dialects.postgresql.partitioning.partition import Partition
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class ExplicitPartitioning(Partitioning):
    """A partitioning whose partitions are listed one by one - ``ListPartitioning`` and
    ``RangePartitioning``: the migrations add (``AddPartition``) and remove (``RemovePartition``)
    each, the default partition too, without rebuilding the table.

    Attributes:
        fields: The fields of the partition key.
        partitions: The partitions - kept in name order, so the order they are listed in doesn't
            matter.
        default_partition: The name of the partition holding the rows no other one holds, None for
            none - a row no partition holds can't be inserted then.
    """

    #: The class of the listed partitions.
    partition_class: ClassVar[type[Partition]]

    partitions: tuple[Any, ...] = ()
    default_partition: str | None = None

    def __post_init__(self) -> None:
        """Checks the key fields and the partitions, and orders the partitions by name.

        Raises:
            ConfigurationError: A key field is wrong, a partition isn't of the strategy's partition
                class, or the default partition's name isn't letters, digits and underscores.
        """
        super().__post_init__()
        partitions = tuple(self.partitions)
        for partition in partitions:
            if not isinstance(partition, self.partition_class):
                raise ConfigurationError(
                    f"{type(self).__name__}: a partition must be a {self.partition_class.__name__}, got {partition!r}"
                )
        object.__setattr__(self, "partitions", tuple(sorted(partitions, key=lambda partition: partition.name)))
        if self.default_partition is not None:
            Partitioning.check_partition_name(type(self).__name__, self.default_partition)

    def get_partitions(self) -> dict[str, Any]:
        partitions: dict[str, Any] = {partition.name: partition for partition in self.partitions}
        if self.default_partition is not None:
            partitions[self.default_partition] = DefaultPartition(self.default_partition)
        return partitions

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        default_names = [
            partition.name for partition in partitions.values() if isinstance(partition, DefaultPartition)
        ]
        if len(default_names) > 1:
            raise ConfigurationError(f"{type(self).__name__}: one default partition at most, got {default_names!r}")
        return dataclasses.replace(
            self,
            partitions=tuple(
                partition for partition in partitions.values() if not isinstance(partition, DefaultPartition)
            ),
            default_partition=default_names[0] if default_names else None,
        )

    def get_partition_names(self) -> list[str]:
        names = [partition.name for partition in self.partitions]
        if self.default_partition is not None:
            names.append(self.default_partition)
        return names

    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        return {partition.name: partition.get_bound_sql(render_value) for partition in self.get_partitions().values()}

    def raise_if_partitions_unsupported(self, model: type[Model], key_column_count: int) -> None:
        """Rejects partitions sharing a name.

        Args:
            model: The partitioned model.
            key_column_count: The number of key columns.

        Raises:
            ConfigurationError: Two partitions share a name.
        """
        Partitioning.raise_if_names_repeat(f"{model.__name__}: {type(self).__name__}", self.get_partition_names())
