from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.statements.select.combined_query import CombinedQuery


class CombinedValuesRows:
    """How the combined rows of ``.values()``/``.values_list()`` queries are read: they take the
    shape of the first query (dicts, tuples, flat values or named tuples), and each column decodes
    through the first branch that knows its field."""

    def __init__(self, first_leaf: ValuesQuery) -> None:
        """
        Args:
            first_leaf: The first ``.values()``/``.values_list()`` query - the rows take its shape.
        """
        self.first_leaf = first_leaf
        #: Per column, its alias and the function decoding its value - worked out by a build.
        self.column_converters: list[tuple[str, Callable[[Any], Any] | None]] = []
        self.column_value_fields: list[Field[Any] | None] = []
        self.has_columns = False

    def get_output_names(self) -> list[str]:
        """The names the combined rows are ordered by - the dict keys, or the selected names."""
        return ValuesOutput.get_output_names_for_set_operation(self.first_leaf)

    def get_output_aliases(self) -> list[str]:
        """The aliases of the combined columns, in output order."""
        return list(ValuesOutput.get_output_aliases(self.first_leaf))

    def get_branch_aliases(self, branch: SelectQuery[Any] | CombinedQuery) -> list[str]:
        """The aliases of a built branch's columns, in output order."""
        if not isinstance(branch, SelectQuery):
            return branch._rows.get_output_aliases()
        return list(ValuesOutput.get_output_aliases(cast("ValuesQuery", branch)))

    def get_default_orderings(self) -> list[tuple[str, Order]]:
        """The ordering of unordered rows ``first()``/``last()`` and ``iterator()`` take - every
        output column."""
        return [(name, Order.ASC) for name in self.get_output_names()]

    def check_ordering_name(self, field_name: str) -> None:
        """Rejects an ordering name that isn't an output name of the rows.

        Raises:
            QueryError: The name isn't one.
        """
        output_names = self.get_output_names()
        if field_name not in output_names:
            raise QueryError(
                f"Cannot order a .values()/.values_list() set operation by {field_name!r} - order by one of "
                f"its output names: {list(output_names)}"
            )

    def get_ordering_alias(self, field_name: str) -> str:
        """The alias of the combined column an ordering name reads."""
        return self.get_output_aliases()[self.get_output_names().index(field_name)]

    def take_branch(self, branch: SelectQuery[Any] | CombinedQuery, branch_index: int) -> None:
        """Checks a built branch selects as many columns as the first one, and takes the decoding
        of the columns the earlier branches left undecoded.

        Raises:
            QueryError: The branch selects another number of columns.
        """
        if not isinstance(branch, SelectQuery):
            nested_rows = cast("CombinedValuesRows", branch._rows)
            converters = nested_rows.column_converters
            value_fields = nested_rows.column_value_fields
        else:
            values_query = cast("ValuesQuery", branch)
            converters = ValuesOutput.get_column_converters(values_query)
            value_fields = [output_field for *_names, output_field in values_query._get_output_columns()]
        if not self.has_columns:
            self.has_columns = True
            self.column_converters = list(converters)
            self.column_value_fields = list(value_fields)
            return
        if len(converters) != len(self.column_converters):
            raise QueryError(
                "Every branch of a .values()/.values_list() set operation must select the same number of "
                f"columns - the first selects {len(self.column_converters)}, branch {branch_index + 1} selects "
                f"{len(converters)}."
            )
        for position, (_alias, converter) in enumerate(converters):
            if self.column_converters[position][1] is None and converter is not None:
                self.column_converters[position] = (self.column_converters[position][0], converter)
            if self.column_value_fields[position] is None:
                self.column_value_fields[position] = value_fields[position]

    def get_result_reading(self) -> tuple[Any, ...]:
        """What reading the rows of the statement just built needs - kept with its plan."""
        return (tuple(self.column_converters), tuple(self.column_value_fields))

    def restore(self, result_reading: tuple[Any, ...]) -> None:
        """Takes what reading the rows needs from the plan the query runs on."""
        converters, value_fields = result_reading
        self.column_converters = list(converters)
        self.column_value_fields = list(value_fields)

    def get_output_columns(self) -> list[tuple[str, str, str, Field[Any] | None]]:
        """The combined columns, to read from them as a derived table - per column its output
        name (twice), its alias and its value field."""
        return [
            (output_name, output_name, alias, value_field)
            for output_name, alias, value_field in zip(
                self.get_output_names(), self.get_output_aliases(), self.column_value_fields, strict=True
            )
        ]

    async def read(self, query: CombinedQuery, sql: str, parameters: list[Any]) -> list[Any]:
        """Runs the statement and builds the rows."""
        return await self.first_leaf._fetch_rows(query._connection, sql, parameters, self.column_converters)

    async def stream_batches(
        self, query: CombinedQuery, connection: DatabaseClient, sql: str, parameters: list[Any], chunk_size: int
    ) -> AsyncIterator[list[Any]]:
        """Streams the rows off a server-side cursor, a batch at a time."""
        async with contextlib.aclosing(connection.stream_batches(sql, parameters, chunk_size=chunk_size)) as batches:
            async for batch in batches:
                yield [self.first_leaf._convert_row(dict(row), self.column_converters) for row in batch]
