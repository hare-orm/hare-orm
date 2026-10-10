from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.dialects.postgresql.enums import RangeBound
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.partitioning.partitions.partition import Partition
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class RangePartition(Partition):
    """A partition of a ``RangePartitioning``: the rows whose key is at least ``from_values`` and
    below ``to_values`` - one value per key column, compared column by column.
    ``RangeBound.MINVALUE``/``RangeBound.MAXVALUE`` stand below/above every value of a column; once
    a bound uses one, its following columns use the same.

    Attributes:
        name: The partition's name - its table is ``<table>_<name>``.
        from_values: The lower bound, included.
        to_values: The upper bound, excluded.
    """

    from_values: tuple[Any, ...] = ()
    to_values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        """Freezes the bounds as tuples and checks the partition.

        Raises:
            ConfigurationError: The name isn't letters, digits and underscores, a bound is empty or
                mixes ``RangeBound`` members wrongly, or the lower bound isn't below the upper one.
        """
        Partitioning.check_partition_name(type(self).__name__, self.name)
        for attribute in ("from_values", "to_values"):
            value = getattr(self, attribute)
            values = tuple(value) if isinstance(value, (list, tuple)) else (value,)
            if not values:
                raise ConfigurationError(f"RangePartition {self.name!r}: {attribute} is empty")
            RangePartition.check_bound(self.name, attribute, values)
            object.__setattr__(self, attribute, values)
        if len(self.from_values) == len(self.to_values) and not (
            RangePartition.get_sort_key(self.from_values) < RangePartition.get_sort_key(self.to_values)
        ):
            raise ConfigurationError(
                f"RangePartition {self.name!r}: from_values {self.from_values!r} isn't below to_values "
                f"{self.to_values!r}"
            )

    @staticmethod
    def check_bound(name: str, attribute: str, values: tuple[Any, ...]) -> None:
        """Checks that a bound continues a ``RangeBound`` member with the same member, as PostgreSQL
        requires.

        Args:
            name: The partition's name, for the message.
            attribute: The bound's attribute, for the message.
            values: The bound.

        Raises:
            ConfigurationError: A column after a ``RangeBound`` member has another value.
        """
        for position, value in enumerate(values):
            if isinstance(value, RangeBound) and any(following != value for following in values[position + 1 :]):
                raise ConfigurationError(
                    f"RangePartition {name!r}: {attribute} {values!r} - every column after {value} must be {value} too"
                )

    @staticmethod
    def get_sort_key(values: tuple[Any, ...]) -> tuple[tuple[Any, ...], ...]:
        """A key ordering bounds as PostgreSQL does: column by column, ``MINVALUE`` below and
        ``MAXVALUE`` above every value.

        Args:
            values: A bound.

        Returns:
            The key.
        """
        return tuple(
            (0,) if value == RangeBound.MINVALUE else (2,) if value == RangeBound.MAXVALUE else (1, value)
            for value in values
        )

    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        def render_bound(values: tuple[Any, ...]) -> str:
            return ", ".join(
                str(value) if isinstance(value, RangeBound) else render_value(position, value)
                for position, value in enumerate(values)
            )

        return f"FOR VALUES FROM ({render_bound(self.from_values)}) TO ({render_bound(self.to_values)})"
