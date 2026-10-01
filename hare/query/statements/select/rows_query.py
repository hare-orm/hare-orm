from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast

from hare.exceptions import QueryError
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class RowsQuery(AwaitableQuery[TModel], abstract=True):
    """Runs the query of a queryset's rows - model instances, ``.values()``/``.values_list()`` rows or
    the rows of a set operation: awaiting, paging by ``iterator()`` and streaming. A subclass says
    how the rows are ordered for paging and read.
    """

    #: A plain SELECT - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    def __await__(self) -> Generator[Any, None, Any]:
        if self._is_none:
            return self._execute_none().__await__()
        query = self._get_execution_query(self._select_for_update)
        query._make_query_to_run()
        return query._execute_with_retry_context(query._execute()).__await__()

    async def _execute_none(self) -> Any:
        return self._empty_single_or_list_result()

    def iterator(self, chunk_size: int = 1000) -> AsyncIterator[Any]:
        """Pages through the rows ``chunk_size`` at a time - see ``iterate_query()``.

        Args:
            chunk_size: The page size.

        Returns:
            The rows.
        """
        return RowsQuery.iterate_query(lambda: self, chunk_size)

    @staticmethod
    async def iterate_query(get_query: Callable[[], RowsQuery[Any]], chunk_size: int) -> AsyncGenerator[Any]:
        """Pages through the rows of the query ``get_query()`` makes once iteration starts,
        ``chunk_size`` at a time - one generator hands out every row. An unordered query follows its
        default iteration order; a tie-breaker makes the ordering unique per row. Pages by keyset
        when every ordering value can be read off a row, else by ``OFFSET``. The query's own slice
        and keyset boundary bound the whole iteration; a page is fetched with one row more,
        which tells whether another page follows.

        Args:
            get_query: Makes the query - an error in making it is raised by the first ``anext()``.
            chunk_size: The page size.

        Raises:
            QueryError: ``chunk_size`` isn't positive, or a ``before_cursor()`` query.
        """
        rows_query = get_query()
        rows_query._check_chunk_size(chunk_size)
        if rows_query._reverse_result_order:
            raise QueryError("iterator() does not support .before_cursor() - await the page instead")
        if rows_query._is_none:
            return
        query = rows_query._get_iterated_query()
        page_template = copy(query)
        page_template._single = False
        page_template._raise_does_not_exist = False
        keyset_readers = page_template._prepare_paging()
        offset = query._offset
        cursor_values = query._cursor_values

        async def fetch_page(page_size: int, probes_next_page: bool) -> tuple[list[Any], bool]:
            nonlocal offset, cursor_values
            page = copy(page_template)
            # One row more than the page tells whether another page follows - the last page then
            # needs no empty query after it.
            page._limit = page_size + 1 if probes_next_page else page_size
            page._offset = offset
            page._cursor_values = cursor_values
            rows = await cast("Awaitable[list[Any]]", page)
            has_next_page = len(rows) > page_size
            if has_next_page:
                rows = rows[:page_size]
            if keyset_readers is None:
                offset = (offset or 0) + page_size
            elif rows:
                # The seek past the last row skips everything before it, the query's own offset
                # included.
                cursor_values = tuple(reader(rows[-1]) for reader in keyset_readers)
                offset = None
            page_template._drop_cursor_values(rows)
            return rows, has_next_page

        remaining = query._limit
        while remaining is None or remaining > 0:
            page_size = chunk_size if remaining is None else min(chunk_size, remaining)
            rows, has_next_page = await fetch_page(page_size, remaining is None or remaining > page_size)
            for row in rows:
                yield row
            if not has_next_page:
                return
            if remaining is not None:
                remaining -= len(rows)

    def _get_iterated_query(self) -> Self:
        """This query in the order ``iterator()`` pages by - its own, else the default one."""
        if self._orderings:
            return self
        query = copy(self)
        query._orderings = self._get_default_iteration_orderings()
        return query

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """The order an unordered query is iterated in."""
        raise NotImplementedError()  # pragma: nocoverage

    def _prepare_paging(self) -> list[Callable[[Any], Any]] | None:
        """Orders this template of ``iterator()``'s pages uniquely per row.

        Returns:
            Readers of each ordering value off a returned row when the pages follow one another by
            keyset, else None - paging by ``OFFSET``.
        """
        self._orderings = self._get_orderings_with_tie_breaker()
        return self._get_keyset_readers()

    def _get_orderings_with_tie_breaker(self) -> list[tuple[str, Order]]:
        """The ordering with the columns appended that make it unique per row."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_keyset_readers(self) -> list[Callable[[Any], Any]] | None:
        """Readers of each ordering value off a returned row, when the pages can follow one
        another by keyset - none by default."""
        return None

    def _drop_cursor_values(self, rows: Sequence[Any]) -> None:
        """Drops from the returned rows the extra columns ``_prepare_paging()`` selected."""

    def _ordering_field_names_are_unique(self, ordering_field_names: set[str]) -> bool:
        """Whether no two rows can share the values of the ordering fields - they include the
        primary key, a non-nullable unique field, or every field of a non-nullable unconditional
        ``UniqueConstraint``."""
        return AggregatedMultiValuedPaths.field_names_identify_row(self.model, ordering_field_names)

    def stream(self, chunk_size: int = 1000) -> AsyncGenerator[Any]:
        """Streams the rows off a server-side cursor (PostgreSQL), inside a transaction - see
        ``QuerySet.stream()``.

        Args:
            chunk_size: How many rows to fetch per round trip.

        Returns:
            The rows.
        """
        return RowsQuery.stream_query(lambda: self, chunk_size)

    @staticmethod
    async def stream_query(get_query: Callable[[], RowsQuery[Any]], chunk_size: int) -> AsyncGenerator[Any]:
        """Streams the rows of the query ``get_query()`` makes once iteration starts - an error in
        making it is raised by the first ``anext()``, like any other.

        Args:
            get_query: Makes the query.
            chunk_size: How many rows to fetch per round trip.

        Raises:
            QueryError: ``chunk_size`` isn't positive.
            UnSupportedError: The database has no server-side streaming (SQLite).
            QueryError: Outside a transaction, a ``before_cursor()`` query, or model instances
                with ``prefetch_related()``.
        """
        rows_query = get_query()
        rows_query._check_chunk_size(chunk_size)
        if rows_query._is_none:
            return
        if rows_query._reverse_result_order:
            raise QueryError("stream() does not support .before_cursor() - await the page instead")
        query = rows_query._get_execution_query(rows_query._select_for_update)
        db = rows_query._check_streamable(query._db)
        query._raise_if_not_streamable()
        query._make_query_to_run()
        batches = query._stream_batches(db, chunk_size)
        # Closing this generator (break + aclose(), cancellation) closes the driver's cursor right
        # away instead of whenever the garbage collector finalizes the inner generators.
        async with contextlib.aclosing(batches):
            async for batch in batches:
                for row in batch:
                    db.check_stream_open()
                    yield row

    def _raise_if_not_streamable(self) -> None:
        """Rejects streaming rows that can't be read one at a time. Rejects none by default."""

    def _stream_batches(self, db: TransactionClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        """The rows of the built statement, read off a server-side cursor a batch at a time."""
        raise NotImplementedError()  # pragma: nocoverage
