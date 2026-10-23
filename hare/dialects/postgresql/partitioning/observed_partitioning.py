from __future__ import annotations

import decimal
from collections.abc import Mapping, Sequence
from typing import Any

from hare.dialects.postgresql.enums import PartitionStrategy, RangeBound
from hare.dialects.postgresql.partitioning.constants import (
    BOUND_VALUE_READERS_BY_COLUMN_TYPE,
    HASH_BOUND_PATTERN,
    LIST_BOUND_PATTERN,
    PARTITION_STRATEGY_BY_CATALOG_CODE,
    PARTITION_VALUE_NOT_SET,
    PARTITIONS_DIFFER_TEMPLATE,
    POSTGRESQL_TYPE_ARGUMENTS_PATTERN,
    RANGE_BOUND_PATTERN,
)
from hare.dialects.postgresql.partitioning.hash_partitioning import HashPartitioning
from hare.dialects.postgresql.partitioning.list_partitioning import ListPartitioning
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.partitioning.partitions.list_partition import ListPartition
from hare.dialects.postgresql.partitioning.partitions.range_partition import RangePartition
from hare.dialects.postgresql.partitioning.range_partitioning import RangePartitioning
from hare.exceptions import ConfigurationError


class ObservedPartitioning:
    """Reads how a table is partitioned off the PostgreSQL catalog - what ``inspectdb`` writes as
    ``PostgresqlTableOptions(partitioning=...)`` and ``hare drift`` compares with the declared one."""

    @staticmethod
    def split_values(text: str) -> list[str]:
        """Splits the list of a bound (``'a', 'b''c', 3``) into its literals.

        Args:
            text: The list's text, without its parentheses.

        Returns:
            The literals, each as written.
        """
        values: list[str] = []
        current: list[str] = []
        in_string = False
        for character in text:
            if character == "'":
                in_string = not in_string
            if character == "," and not in_string:
                values.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        if current or values:
            values.append("".join(current).strip())
        return values

    @staticmethod
    def read_value(literal: str, column_type: str) -> Any:
        """The Python value of a bound's literal for a key column.

        Args:
            literal: The literal as ``pg_get_expr()`` writes it.
            column_type: The key column's type, as ``format_type()`` names it.

        Returns:
            The value - a ``RangeBound`` member for ``MINVALUE``/``MAXVALUE``, None for ``NULL``.
        """
        if literal in {RangeBound.MINVALUE, RangeBound.MAXVALUE}:
            return RangeBound(literal)
        if literal.upper() == "NULL":
            return None
        text = literal
        if text.startswith("'"):
            # 'it''s'::text - the literal, then a cast PostgreSQL may write after it.
            text = text[1 : text.rindex("'")].replace("''", "'")
        base_type = POSTGRESQL_TYPE_ARGUMENTS_PATTERN.sub("", column_type).strip()
        if base_type == "boolean":
            return text.lower() in {"true", "t"}
        reader = BOUND_VALUE_READERS_BY_COLUMN_TYPE.get(base_type)
        return reader(text) if reader is not None else text

    @staticmethod
    def read_values(text: str, column_types: Sequence[str]) -> tuple[Any, ...]:
        """The values of a bound's list, one per key column in order - a ``LIST`` bound lists
        values of the one key column.

        Args:
            text: The list's text, without its parentheses.
            column_types: The key columns' types.

        Returns:
            The values.
        """
        literals = ObservedPartitioning.split_values(text)
        if len(column_types) == 1:
            return tuple(ObservedPartitioning.read_value(literal, column_types[0]) for literal in literals)
        return tuple(
            ObservedPartitioning.read_value(literal, column_type)
            for literal, column_type in zip(literals, column_types, strict=True)
        )

    @staticmethod
    def get_partition_name(table_name: str, partition_table_name: str) -> str:
        """The name of a partition by its table: what follows ``<table>_``, the whole table name
        when it isn't named after the table.

        Args:
            table_name: The partitioned table.
            partition_table_name: The partition's table.

        Returns:
            The name.
        """
        return partition_table_name.removeprefix(f"{table_name}_")

    @staticmethod
    def build(table_name: str, rows: Sequence[Mapping[str, Any]]) -> Partitioning | None:
        """The partitioning of a table from its rows of the partitions query: one row per
        partition, each with the table's strategy, key columns and their types; a table without
        partitions has one row with no partition.

        Args:
            table_name: The table.
            rows: The rows.

        Returns:
            The partitioning, None when the table isn't partitioned or hare can't declare how it
            is - a key over an expression, hash partitions of different moduli.
        """
        if not rows:
            return None
        first_row = rows[0]
        key_columns = tuple(first_row["key_columns"])
        column_types = list(first_row["key_types"])
        if len(key_columns) != first_row["key_column_count"]:
            return None
        strategy = PARTITION_STRATEGY_BY_CATALOG_CODE.get(first_row["strategy"])
        partition_rows = [row for row in rows if row["partition_table"] is not None]
        default_partition: str | None = None
        partitions: list[Any] = []
        moduli: set[int] = set()
        try:
            for row in partition_rows:
                name = ObservedPartitioning.get_partition_name(table_name, row["partition_table"])
                bound = row["bound"]
                if bound == "DEFAULT":
                    default_partition = name
                elif strategy == PartitionStrategy.HASH and (match := HASH_BOUND_PATTERN.fullmatch(bound)):
                    moduli.add(int(match["modulus"]))
                elif strategy == PartitionStrategy.LIST and (match := LIST_BOUND_PATTERN.fullmatch(bound)):
                    values = ObservedPartitioning.read_values(match["values"], column_types)
                    partitions.append(ListPartition(name, values=values))
                elif strategy == PartitionStrategy.RANGE and (match := RANGE_BOUND_PATTERN.fullmatch(bound)):
                    partitions.append(
                        RangePartition(
                            name,
                            from_values=ObservedPartitioning.read_values(match["from_values"], column_types),
                            to_values=ObservedPartitioning.read_values(match["to_values"], column_types),
                        )
                    )
                else:
                    return None
            if strategy == PartitionStrategy.HASH:
                if len(moduli) != 1 or moduli != {len(partition_rows)}:
                    return None
                return HashPartitioning(fields=key_columns, partition_count=len(partition_rows))
            if strategy == PartitionStrategy.LIST:
                return ListPartitioning(
                    fields=key_columns, partitions=tuple(partitions), default_partition=default_partition
                )
            if strategy == PartitionStrategy.RANGE:
                return RangePartitioning(
                    fields=key_columns, partitions=tuple(partitions), default_partition=default_partition
                )
        except (ConfigurationError, ValueError, decimal.InvalidOperation):
            # A partition hare has no declaration for - a name or a value it can't write.
            return None
        return None

    @staticmethod
    def merge_partition_values(values_by_partition: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """One set of values for a partitioned table from the values each partition has - its
        storage parameters: a value every partition shares as it is, one they don't as a text
        naming each partition's value, so a comparison with the declared value shows it.

        Args:
            values_by_partition: Each partition's values by name.

        Returns:
            The merged values.
        """
        names = sorted({name for values in values_by_partition.values() for name in values})
        merged: dict[str, Any] = {}
        for name in names:
            partition_values = {
                partition: values.get(name, PARTITION_VALUE_NOT_SET)
                for partition, values in values_by_partition.items()
            }
            distinct_values = {repr(value) for value in partition_values.values()}
            if len(distinct_values) == 1:
                merged[name] = next(iter(partition_values.values()))
            else:
                merged[name] = PARTITIONS_DIFFER_TEMPLATE.format(
                    values=", ".join(f"{partition}={value}" for partition, value in sorted(partition_values.items()))
                )
        return merged
