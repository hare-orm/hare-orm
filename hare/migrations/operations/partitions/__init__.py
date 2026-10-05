"""Operations on the partitions of a model's partitioned table."""

from __future__ import annotations

from hare.migrations.operations.partitions.add_partition import AddPartition
from hare.migrations.operations.partitions.remove_partition import RemovePartition

__all__ = [
    "AddPartition",
    "RemovePartition",
]
