from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError
from hare.query.execution.query_execution import QueryExecution
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql import Order

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.fields.field import Field
    from hare.query.statements.projections.declarations import SelectedOutputColumn
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
        return list(self.first_leaf._selected_fields_by_alias)

    def get_branch_aliases(self, branch: SelectQuery[Any] | CombinedQuery) -> list[str]:
        """The aliases of a built branch's columns, in output order."""
        if not isinstance(branch, SelectQuery):
            return branch._rows.get_output_aliases()
        return list(ValuesOutput.get_output_aliases(branch))  # type: ignore[arg-type]

    def get_default_orderings(self) -> list[tuple[str, Order]]:
        """The ordering of unordered rows ``first()``/``last()`` and ``iterator()`` take - every
        output column."""
        return [(name, Order.ASC) for name in self.get_output_names()]

    def get_ordering_aliases(self, field_name: str) -> tuple[str, ...]:
        """Expand a logical output to each of its columns in component order."""
        aliases = self.first_leaf._rows.get_composite_output_aliases().get(field_name)
        return aliases or (self.get_ordering_alias(field_name),)

    def check_ordering_name(self, field_name: str) -> None:
        """Rejects an ordering name that isn't an output name of the rows.

        Raises:
            QueryError: The name isn't one.
        """
        output_names = self.get_output_names()
        if field_name not in output_names and field_name not in self.first_leaf._rows.get_composite_output_aliases():
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
            nested_rows: CombinedValuesRows = branch._rows  # type: ignore[assignment]
            converters = nested_rows.column_converters
            value_fields = nested_rows.column_value_fields
        else:
            values_query: ValuesQuery = branch  # type: ignore[assignment]
            converters = ValuesOutput.get_column_converters(values_query)
            value_fields_by_alias: dict[str | None, Field[Any] | None] = {}
            for column in values_query._get_output_columns():
                value_fields_by_alias.setdefault(column[2], column[3])
            value_fields = [value_fields_by_alias[alias] for alias, _converter in converters]
        if not self.has_columns:
            self.has_columns = True
            self.column_converters = [
                (alias, converter)
                for alias, (_source_alias, converter) in zip(self.get_output_aliases(), converters, strict=True)
            ]
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
        """The immutable column readers; declaration fields belong only to the build."""
        return tuple(self.column_converters)

    def get_output_columns(self) -> list[SelectedOutputColumn]:
        """The combined columns, to read from them as a derived table - per column its output
        name (twice), its alias and its value field."""
        return [
            (output_name, output_name, alias, value_field)
            for output_name, alias, value_field in zip(
                self.get_output_names(), self.get_output_aliases(), self.column_value_fields, strict=True
            )
        ]

    async def read(
        self, query: CombinedQuery, sql: str, parameters: list[Any], result_reading: tuple[Any, ...]
    ) -> list[Any]:
        """Runs the statement through the readers supplied by its plan or completed build."""
        first_leaf = self.first_leaf
        rows = first_leaf._rows
        connection = query._connection
        result = await QueryExecution.fetch_rows(connection, sql, parameters, retry_read=first_leaf.is_read_only)
        return rows.convert_batch(connection, result, result_reading)

    def stream_batches(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        chunk_size: int,
        result_reading: tuple[Any, ...],
        *,
        context: HareContext | None = None,
    ) -> AsyncGenerator[list[Any]]:
        """Stream values through the common combined-reader interface.

        The execution context is an input of model hydration; value shapes construct no
        model instances and need no context in addition to their prepared decoders.
        """
        return self.first_leaf._rows.stream_batches(connection, sql, parameters, result_reading, chunk_size)
