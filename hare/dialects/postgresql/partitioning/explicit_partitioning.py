from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from operator import attrgetter
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.postgresql.partitioning.partitioning import BoundValueRenderer, Partitioning
from hare.dialects.postgresql.partitioning.partitions.default_partition import DefaultPartition
from hare.dialects.postgresql.partitioning.partitions.partition import Partition
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


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
    default_partition: DefaultPartition | str | None = None

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
        object.__setattr__(self, "partitions", tuple(sorted(partitions, key=attrgetter("name"))))
        if isinstance(self.default_partition, str):
            Partitioning.check_partition_name(type(self).__name__, self.default_partition)
        elif self.default_partition is not None and not isinstance(self.default_partition, DefaultPartition):
            raise ConfigurationError(f"{type(self).__name__}: invalid default partition {self.default_partition!r}")

    def get_partitions(self) -> dict[str, Any]:
        partitions: dict[str, Any] = {partition.name: partition for partition in self.partitions}
        default = self.default_partition
        if default is not None:
            if isinstance(default, str):
                default = DefaultPartition(default)
            partitions[default.name] = default
        return partitions

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        defaults = [partition for partition in partitions.values() if isinstance(partition, DefaultPartition)]
        if len(defaults) > 1:
            raise ConfigurationError(
                f"{type(self).__name__}: one default partition at most, got "
                f"{ValueDescriptions.get_shown_value([partition.name for partition in defaults])}"
            )
        default = next(iter(defaults), None)
        return dataclasses.replace(
            self,
            partitions=tuple(
                partition for partition in partitions.values() if not isinstance(partition, DefaultPartition)
            ),
            default_partition=default.name
            if default is not None and default.table is None and default.schema is None
            else default,
        )

    def get_partition_names(self) -> list[str]:
        names = [partition.name for partition in self.partitions]
        default = self.default_partition
        if default is not None:
            names.append(default if isinstance(default, str) else default.name)
        return names

    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        return {partition.name: partition.get_bound_sql(render_value) for partition in self.get_partitions().values()}

    def raise_if_partitions_unsupported(self, binding: ModelBinding, key_column_count: int) -> None:
        """Rejects partitions sharing a name.

        Args:
            binding: The partitioned model binding.
            key_column_count: The number of key columns.

        Raises:
            ConfigurationError: Two partitions share a name.
        """
        model = binding._model
        Partitioning.raise_if_names_repeat(f"{model.__name__}: {type(self).__name__}", self.get_partition_names())
