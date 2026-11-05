from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.results.statement_result import StatementResult
from hare.query.enums import RowShape
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient

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
        column_converters: list[ColumnConverter],
        *,
        retry_read: bool = False,
    ) -> list[Any]:
        """Fetch and materialize through the same layout used by streamed batches."""
        result = await self.fetch_result(connection, sql, parameters, retry_read=retry_read)
        return self.convert_batch(connection, result, column_converters)

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
        column_converters: list[ColumnConverter],
    ) -> list[Any]:
        """Decode one batch, selecting column positions once for the whole batch."""
        if not result.rows:
            return []
        reader = self.get_result_reader(connection, result, column_converters)
        return [reader(row) for row in result.rows]

    async def stream_batches(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: list[ColumnConverter],
        chunk_size: int,
    ) -> AsyncGenerator[list[Any]]:
        """Stream through one decoder bound to the first nonempty batch's column metadata.

        All batches belong to the same driver result. Closing or failing the consumer
        closes the underlying cursor, including when no nonempty batch was received.
        """
        reader = None
        async with contextlib.aclosing(connection.stream_batches(sql, parameters, chunk_size=chunk_size)) as batches:
            async for rows in batches:
                if not rows:
                    yield []
                    continue
                if reader is None:
                    reader = self.get_result_reader(connection, StatementResult(len(rows), rows), column_converters)
                yield [reader(row) for row in rows]

    def get_result_reader(
        self,
        connection: DatabaseClient,
        result: StatementResult,
        column_converters: list[ColumnConverter],
    ) -> Callable[[Any], Any]:
        """Bind one decoder for ordinary and additional rows of the same driver result."""
        layout = self.get_layout(
            column_converters,
            result.column_names if connection.features.supports_positional_rows else None,
        )
        return partial(self.read_row, layout=layout)

    def read_row(self, row: Any, *, layout: RowLayout) -> Any:
        """Apply the bound column layout and assemble the requested row shape."""
        return self.make_row(layout.read(row))

    def get_layout(
        self,
        column_converters: list[ColumnConverter],
        column_names: Sequence[str] | None = None,
    ) -> RowLayout:
        """Bind selected aliases to this result's keys, including composite groups."""
        columns = tuple(
            RowColumn(column_names.index(alias) if column_names is not None else alias, converter)
            for alias, converter in column_converters
        )
        groups = tuple(len(aliases) for aliases in self.output_column_aliases) if self.has_composite_outputs else ()
        return RowLayout(columns, groups)

    def make_row(self, values: tuple[Any, ...]) -> Any:
        """Assemble an already decoded row in the requested result shape."""
        raise NotImplementedError()

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        """Reads the value of a selected column off one row.

        Args:
            position: The column's position among the selected ones.
            alias: Its alias.

        Returns:
            The reader.
        """
        raise NotImplementedError()
