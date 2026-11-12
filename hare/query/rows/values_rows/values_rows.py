from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.base.results.statement_result import StatementResult
from hare.native.native_modules import NativeModules
from hare.query.enums import RowShape
from hare.query.execution.query_execution import QueryExecution
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.queryset.query_specification import QuerySpecification

#: A selected column's alias (or its position in a driver row) with the function decoding its
#: value, None when the raw value is used as-is.
ColumnConverter = tuple[Any, Callable[[Any], Any] | None]


class ValuesRows:
    """How a row of the values ``.values()``/``.values_list()`` select is read - one class per
    shape of a row. A composite key selects one column per key field, returned combined as one
    tuple (None when every column is NULL)."""

    #: The shape the plan key of the query holds - shapes selecting the same columns share one.
    plan_shape: ClassVar[RowShape]
    #: Whether a column is selected under its output key rather than its position - a row is then
    #: read by key, and an annotation the row doesn't name is left out of the SELECT list.
    keyed_by_output_name: ClassVar[bool] = False
    row_class: type[tuple[Any, ...]] | None = None
    #: Layouts follow this reader's lifetime and are invalidated with SQL plans.
    layouts: ClassVar[Cache[tuple[Sequence[ColumnConverter], RowLayout]]] = Cache()

    def __init__(self, output_names: tuple[str, ...], output_column_aliases: Sequence[tuple[str, ...]]) -> None:
        """
        Args:
            output_names: The names of a row's values as returned - the dict keys, the selected
                names of a tuple.
            output_column_aliases: Per returned value, the aliases of its columns.
        """
        self.output_names = output_names
        self.output_column_aliases = output_column_aliases
        self.has_composite_outputs = any(len(column_aliases) > 1 for column_aliases in output_column_aliases)
        self.layout_bucket = self.layouts.new_shared_bucket()

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        """The aliases the columns of one returned value are selected under.

        Args:
            output_name: The value's output name.
            first_column_index: How many columns are selected before it.
            column_count: How many columns the value selects - more than one for a composite key.

        Returns:
            The aliases.
        """
        raise NotImplementedError()

    async def fetch(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: Sequence[ColumnConverter],
        *,
        retry_read: bool = False,
    ) -> list[Any]:
        """Fetch and materialize through the same layout used by streamed batches."""
        result = await self.fetch_result(connection, sql, parameters, retry_read=retry_read)
        return self.convert_batch(connection, result, column_converters)

    async def read(
        self,
        specification: QuerySpecification[Any],
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: Sequence[ColumnConverter],
        *,
        retry_read: bool = False,
        layout: RowLayout | None = None,
    ) -> Any:
        """Runs a query and reads its rows - the one road of a read of values, on a plan found by its
        calls or on a query just built - with the query's read checks (``QueryExecution.check_read_lock()``,
        ``raise_read_error()``).

        Args:
            specification: The query read.
            connection: The connection it reads on.
            sql: The statement.
            parameters: Its parameters.
            column_converters: How each selected column is read.
            retry_read: Whether a lost connection may retry the read.

        Returns:
            The rows - or the one row of a single-row query.
        """
        QueryExecution.check_read_lock(specification, connection)
        try:
            result = await self.fetch_result(connection, sql, parameters, retry_read=retry_read)
            rows = self.convert_batch(connection, result, column_converters, layout=layout)
        except Exception as error:
            QueryExecution.raise_read_error(connection, error)
            raise
        return GetExceptions.single_or_list_result(specification, rows)

    @staticmethod
    async def fetch_result(
        connection: DatabaseClient, sql: str, parameters: list[Any], *, retry_read: bool = False
    ) -> StatementResult:
        """Execute once and preserve driver metadata alongside the ordinary rows."""
        return await connection.execute(
            sql,
            parameters,
            returns_rows=True,
            rows_by_position=connection.features.supports_positional_rows,
            retry_read=retry_read,
        )

    def convert_batch(
        self,
        connection: DatabaseClient,
        result: StatementResult,
        column_converters: Sequence[ColumnConverter],
        *,
        layout: RowLayout | None = None,
    ) -> list[Any]:
        """Decode one batch, selecting column positions once for the whole batch."""
        if not result.rows:
            return []
        if layout is None:
            layout = self.get_layout(
                column_converters, result.column_names if connection.features.supports_positional_rows else None
            )
        return NativeModules.rows.read_values_batch(
            result.rows, layout, self.plan_shape, self.output_names, self.row_class
        )

    async def stream_batches(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: Sequence[ColumnConverter],
        chunk_size: int,
    ) -> AsyncGenerator[list[Any]]:
        """Stream through one decoder bound to the first nonempty batch's column metadata.

        All batches belong to the same driver result. Closing or failing the consumer
        closes the underlying cursor, including when no nonempty batch was received.
        """
        layout = None
        async with contextlib.aclosing(connection.stream_batches(sql, parameters, chunk_size=chunk_size)) as batches:
            async for rows in batches:
                if not rows:
                    yield []
                    continue
                if layout is None:
                    layout = self.get_layout(
                        column_converters,
                        StatementResult(len(rows), rows).column_names
                        if connection.features.supports_positional_rows
                        else None,
                    )
                yield NativeModules.rows.read_values_batch(
                    rows, layout, self.plan_shape, self.output_names, self.row_class
                )

    def get_result_reader(
        self,
        connection: DatabaseClient,
        result: StatementResult,
        column_converters: Sequence[ColumnConverter],
    ) -> Callable[[Any], Any]:
        """Bind one decoder for ordinary and additional rows of the same driver result."""
        layout = self.get_layout(
            column_converters,
            result.column_names if connection.features.supports_positional_rows else None,
        )
        return partial(
            NativeModules.rows.read_values_row,
            layout=layout,
            row_type=self.plan_shape,
            names=self.output_names,
            row_class=self.row_class,
        )

    def get_layout(
        self,
        column_converters: Sequence[ColumnConverter],
        column_names: Sequence[str] | None = None,
    ) -> RowLayout:
        """Bind aliases once per plan conversion and column order, including composite groups.

        Retaining the converter list alongside its identity prevents identity reuse and
        permits callable readers that are unhashable. Only column metadata is retained.
        """
        layout_key = (id(column_converters), None if column_names is None else tuple(column_names))
        cached = self.layout_bucket.get(layout_key)
        if cached is not None:
            return cached[1]
        columns = tuple(
            RowColumn(column_names.index(alias) if column_names is not None else alias, converter)
            for alias, converter in column_converters
        )
        groups = tuple(len(aliases) for aliases in self.output_column_aliases) if self.has_composite_outputs else ()
        layout = RowLayout(columns, groups)
        self.layout_bucket[layout_key] = (column_converters, layout)
        return layout

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        """Reads the value of a selected column off one row.

        Args:
            position: The column's position among the selected ones.
            alias: Its alias.

        Returns:
            The reader.
        """
        raise NotImplementedError()
