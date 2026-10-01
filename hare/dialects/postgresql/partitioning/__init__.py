"""Declarative partitioning of PostgreSQL tables - ``PostgresqlTableOptions(partitioning=...)``."""

from hare.dialects.postgresql.enums import RangeBound
from hare.dialects.postgresql.partitioning.default_partition import DefaultPartition
from hare.dialects.postgresql.partitioning.hash_partitioning import HashPartitioning
from hare.dialects.postgresql.partitioning.list_partition import ListPartition
from hare.dialects.postgresql.partitioning.list_partitioning import ListPartitioning
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.partitioning.range_partition import RangePartition
from hare.dialects.postgresql.partitioning.range_partitioning import RangePartitioning

__all__ = [
    "DefaultPartition",
    "HashPartitioning",
    "ListPartition",
    "ListPartitioning",
    "Partitioning",
    "RangeBound",
    "RangePartition",
    "RangePartitioning",
]
