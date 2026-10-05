from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from hare.dialects.clickhouse.client.clickhouse_query_templates import ClickhouseQueryTemplates
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_EXTERNAL_SET_COLUMN_TEMPLATE,
    CLICKHOUSE_EXTERNAL_SET_NAME_TEMPLATE,
    CLICKHOUSE_TAB_SEPARATED_ESCAPES,
    CLICKHOUSE_TAB_SEPARATED_SPECIAL_CHARACTERS,
)
from hare.dialects.clickhouse.client.declarations import ClickhouseExternalSet, ClickhouseValueSet


class ClickhouseExternalSets:
    """The long ``IN`` lists of a read sent as external tables - a list bound as one
    ``ClickhouseValueSet`` parameter is written ``x IN hare_set_1``, its values sent beside the statement
    as data: the server parses a list of 100,000 literals for most of a second and reads the same values
    as data in a few milliseconds. In any other statement the parameter is written as a list of
    literals."""

    @staticmethod
    def get_query(
        query: str, values: Sequence[Any], get_literal_sql: Callable[[Any], str]
    ) -> tuple[str, list[ClickhouseExternalSet]]:
        """A read with its sets of values sent as external tables.

        Args:
            query: The statement.
            values: The values, the ``n``-th for ``$n``.
            get_literal_sql: Writes a value's literal.

        Returns:
            The statement to send - the other values written as literals - and the external tables.
        """
        text, indexes = ClickhouseQueryTemplates.get_cached_template(query)
        external_sets: list[ClickhouseExternalSet] = []
        names_by_index: dict[int, str] = {}
        arguments: list[str] = []
        for index in indexes:
            value = values[index]
            if type(value) is not ClickhouseValueSet:
                arguments.append(get_literal_sql(value))
                continue
            name = names_by_index.get(index)
            if name is None:
                name = names_by_index[index] = CLICKHOUSE_EXTERNAL_SET_NAME_TEMPLATE.format(len(external_sets) + 1)
                external_sets.append(ClickhouseExternalSet(name, value))
            arguments.append(name)
        return text.format(*arguments), external_sets

    @staticmethod
    def get_structure(value_set: ClickhouseValueSet) -> list[tuple[str, str]]:
        """The columns of an external table - one per value of a row.

        Args:
            value_set: The values.

        Returns:
            The name and the type of each column.
        """
        return [
            (CLICKHOUSE_EXTERNAL_SET_COLUMN_TEMPLATE.format(position), column_type)
            for position, column_type in enumerate(value_set.column_types, 1)
        ]

    @staticmethod
    def get_tab_separated_data(value_set: ClickhouseValueSet) -> bytes:
        """The values of an external table in the ``TabSeparated`` format - one line per row.

        Args:
            value_set: The values.

        Returns:
            The data.
        """
        texts = [column_type == "String" for column_type in value_set.column_types]
        if len(texts) == 1:
            if not texts[0]:
                return "\n".join(map(str, value_set.rows)).encode()
            data = "\n".join(value_set.rows)
            # Escaped only where a text holds a character the format escapes.
            if data.count("\n") == len(value_set.rows) - 1 and not any(
                character in data for character in CLICKHOUSE_TAB_SEPARATED_SPECIAL_CHARACTERS
            ):
                return data.encode()
            return "\n".join(value.translate(CLICKHOUSE_TAB_SEPARATED_ESCAPES) for value in value_set.rows).encode()
        return "\n".join(
            "\t".join(
                value.translate(CLICKHOUSE_TAB_SEPARATED_ESCAPES) if is_text else str(value)
                for value, is_text in zip(row, texts, strict=True)
            )
            for row in value_set.rows
        ).encode()

    @staticmethod
    def get_rows(value_set: ClickhouseValueSet) -> list[tuple[Any, ...]]:
        """The rows of an external table.

        Args:
            value_set: The values.

        Returns:
            Each row as a tuple.
        """
        if len(value_set.column_types) == 1:
            return [(value,) for value in value_set.rows]
        return value_set.rows
