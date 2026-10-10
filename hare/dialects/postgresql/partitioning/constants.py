from __future__ import annotations

import datetime
import decimal
import re
import uuid
from collections.abc import Callable
from typing import Any

from hare.dialects.postgresql.enums import PartitionStrategy

#: The most partitions a ``HashPartitioning`` creates - every partition is a table of its own, and
#: a statement planned against thousands of them gets slow on PostgreSQL.
HASH_PARTITION_COUNT_LIMIT = 1024

#: The name of a hash partition of the remainder it holds - its table is ``<table>_p<remainder>``.
HASH_PARTITION_NAME_TEMPLATE = "p{remainder}"

#: How a partition's name is written: the characters of an unquoted identifier.
PARTITION_NAME_PATTERN = r"[A-Za-z0-9_]+"

#: The observed value of a storage parameter or tablespace the partitions of a table don't share.
PARTITIONS_DIFFER_TEMPLATE = "differs between partitions: {values}"

#: How a partition without the storage parameter is written in ``PARTITIONS_DIFFER_TEMPLATE``.
PARTITION_VALUE_NOT_SET = "(not set)"

#: ``pg_partitioned_table.partstrat`` -> the strategy.
PARTITION_STRATEGY_BY_CATALOG_CODE: dict[str, PartitionStrategy] = {
    "h": PartitionStrategy.HASH,
    "l": PartitionStrategy.LIST,
    "r": PartitionStrategy.RANGE,
}

#: A partition's bound as ``pg_get_expr(relpartbound)`` writes it, by strategy.
HASH_BOUND_PATTERN = re.compile(r"FOR VALUES WITH \(modulus (?P<modulus>\d+), remainder (?P<remainder>\d+)\)")
LIST_BOUND_PATTERN = re.compile(r"FOR VALUES IN \((?P<values>.*)\)", re.DOTALL)
RANGE_BOUND_PATTERN = re.compile(r"FOR VALUES FROM \((?P<from_values>.*)\) TO \((?P<to_values>.*)\)", re.DOTALL)

#: Reads a bound value of a key column by the column's type, as ``format_type()`` names it - a
#: type not listed keeps the value's text.
BOUND_VALUE_READERS_BY_COLUMN_TYPE: dict[str, Callable[[str], Any]] = {
    "smallint": int,
    "integer": int,
    "bigint": int,
    "numeric": decimal.Decimal,
    "real": float,
    "double precision": float,
    "date": datetime.date.fromisoformat,
    "timestamp without time zone": datetime.datetime.fromisoformat,
    "timestamp with time zone": datetime.datetime.fromisoformat,
    "uuid": uuid.UUID,
}

#: The arguments of a column type - ``(10,2)`` of ``numeric(10,2)``.
POSTGRESQL_TYPE_ARGUMENTS_PATTERN = re.compile(r"\(.*\)")
