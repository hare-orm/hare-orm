from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

from hare.exceptions import QueryError
from hare.fields.data.constants import INT32_MAX
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql import Order

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.plans.description.plan_description import PlanDescription
    from hare.query.plans.statement.declarations import CallPlanHit
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.values.value_wrapper import ValueWrapper

TModel = TypeVar("TModel", bound="Model")


class RowsQuery(AwaitableQuery[TModel], abstract=True):
    """Runs the query of a queryset's rows - model instances, ``.values()``/``.values_list()`` rows or
    the rows of a set operation: awaiting, paging by ``iterator()`` and streaming. A subclass says
    how the rows are ordered for paging and read.
    """

    #: A plain SELECT - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    def _prepare_slice_terms(
        self, query: QueryBuilder, offset: int | None
    ) -> tuple[ValueWrapper | None, ValueWrapper | None]:
        """Build the same slice terms used by SQL and recorded value references."""
        wrapper = query._wrapper_class
        terms = (
            wrapper(self._limit) if self._limit is not None else None,
            wrapper(offset) if offset is not None else None,
        )
        self._slice_boundary_terms = terms
        return terms

    def _record_slice_values(
        self, references: RecordedValueReferences, terms: tuple[ValueWrapper | None, ValueWrapper | None]
    ) -> None:
        for name, term in zip(("_limit", "_offset"), terms, strict=True):
            if term is not None:
                ExpressionArguments.record_literal(references, self, name, term)

    def _get_build_plan_description(self) -> PlanDescription | None:
        return self._describe_statement(False)

    def _needs_dependency_values(self) -> bool:
        # Extensions may change the slice; ordinary zero-limit reads consume no inputs.
        return super()._needs_dependency_values() and (self._limit != 0 or bool(self._extension_calls))

    async def _execute_bound(self) -> Any:
        if self._is_none:
            return GetExceptions.empty_single_or_list_result(self)
        query = self._get_execution_query(self._select_for_update)
        statement = query._make_query_to_run()
        result = await query._execute(statement)
        return (
            QuerySetExtensions.transform_result(query, result)
            if query._extension_calls and query._applies_result_transform
            else result
        )

    def _execute(self, statement: tuple[str, list[Any]]) -> Coroutine[Any, Any, Any]:
        """The read of the built statement - the one road a plan found by the calls takes too
        (``read_call_plan()``), with the read checks of ``QueryExecution``."""
        raise NotImplementedError

    @staticmethod
    def read_call_plan(
        queryset: QuerySet[Any, Any], connection: DatabaseClient, hit: CallPlanHit
    ) -> Coroutine[Any, Any, Any]:
        """Reads a queryset's rows on the plan of its calls, with no query object made.

        Args:
            queryset: The queryset.
            connection: This execution's connection.
            hit: The plan found (``StatementPlanRuns.find_call_plan()``).
        """
        raise NotImplementedError()

    def iterator(self, chunk_size: int = 1000) -> AsyncIterator[Any]:
        """Pages through the rows ``chunk_size`` at a time - see ``iterate_query()``.

        Args:
            chunk_size: The page size.

        Returns:
            The rows.
        """
        return RowsQuery.iterate_query(self, chunk_size)

    @staticmethod
    async def iterate_query(specification: QuerySpecification[Any], chunk_size: int) -> AsyncGenerator[Any]:
        """Read with one set of dependency inputs throughout the iterator's lifetime."""
        source = QueryExecution.get_bound(specification)
        with QueryExecution(source._context):
            query = source._get_compiler()
        query._check_chunk_size(chunk_size)
        iterator = QueryExecution.iterate(query, RowsQuery._iterate_rows(query, chunk_size))
        async with contextlib.aclosing(iterator):
            async for page in iterator:
                for row in page:
                    yield row

    @staticmethod
    async def _iterate_rows(rows_query: RowsQuery[Any], chunk_size: int) -> AsyncGenerator[list[Any]]:
        """Pages through the rows of the prepared query,
        ``chunk_size`` at a time - one generator hands out every row. An unordered query follows its
        default iteration order; a tie-breaker makes the ordering unique per row. Pages by keyset
        when the query provides supported keyset readers, else by ``OFFSET``. The query's own slice
        and keyset boundary bound the whole iteration; a page is fetched with one row more,
        which tells whether another page follows.

        Args:
            rows_query: The query created when iteration starts.
            chunk_size: The validated page size.

        Raises:
            QueryError: The query uses ``before_cursor()``.
        """
        if rows_query._reverse_result_order:
            raise QueryError("iterator() does not support .before_cursor() - await the page instead")
        if rows_query._is_none:
            return
        query: RowsQuery[Any]
        bound_source = (
            rows_query._get_execution_query(rows_query._select_for_update) if rows_query._extension_calls else None
        )
        if bound_source is not None and QuerySetExtensions.changes_rows(bound_source):
            from hare.query.statements.select.iterator_page_query import IteratorPageQuery

            query = IteratorPageQuery(bound_source)
        else:
            query = rows_query._get_iterated_query()
        page_template = query._get_execution_query(query._select_for_update)
        page_template._applies_result_transform = False
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
            rows = await page
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
            yield rows
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
        raise NotImplementedError()

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
        raise NotImplementedError()

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
        model_binding: ModelBinding = self._binding  # type: ignore[assignment]
        return AggregatedMultiValuedPaths.field_names_identify_row(model_binding, ordering_field_names)

    def stream(self, chunk_size: int = 1000) -> AsyncGenerator[Any]:
        """Streams the rows off a cursor, inside a transaction - see
        ``QuerySet.stream()``.

        Args:
            chunk_size: How many rows to fetch per round trip.

        Returns:
            The rows.
        """
        return RowsQuery.stream_query(self, chunk_size)

    @staticmethod
    async def stream_query(specification: QuerySpecification[Any], chunk_size: int) -> AsyncGenerator[Any]:
        """Read with one set of dependency inputs throughout the iterator's lifetime."""
        source = QueryExecution.get_bound(specification)
        with QueryExecution(source._context):
            query = source._get_compiler()
        query._check_chunk_size(chunk_size, INT32_MAX)
        iterator = QueryExecution.iterate(query, RowsQuery._stream_rows(query, chunk_size))
        async with contextlib.aclosing(iterator):
            async for row in iterator:
                yield row

    @staticmethod
    async def _stream_rows(rows_query: RowsQuery[Any], chunk_size: int) -> AsyncGenerator[Any]:
        """Stream an already created query with its dependency inputs bound.

        Args:
            rows_query: The query created when iteration starts.
            chunk_size: The validated number of rows to fetch per round trip.

        Raises:
            UnSupportedError: The database has no streaming (``features.supports_streaming``).
            QueryError: Outside a transaction, a ``before_cursor()`` query, or model instances
                with ``prefetch_related()``.
        """
        if rows_query._is_none:
            return
        if rows_query._reverse_result_order:
            raise QueryError("stream() does not support .before_cursor() - await the page instead")
        query = rows_query._get_execution_query(rows_query._select_for_update)
        connection = rows_query._check_streamable(query._connection)
        query._raise_if_not_streamable()
        statement = query._make_query_to_run()
        batches = query._stream_batches(connection, chunk_size, statement)
        # Closing this generator (break + aclose(), cancellation) closes the driver's cursor right
        # away instead of whenever the garbage collector finalizes the inner generators.
        async with contextlib.aclosing(batches):
            async for batch in batches:
                for row in batch:
                    connection.check_stream_open()
                    yield row

    def _raise_if_not_streamable(self) -> None:
        """Rejects streaming rows that can't be read one at a time. Rejects none by default."""

    def _stream_batches(
        self, connection: DatabaseClient, chunk_size: int, statement: tuple[str, list[Any]]
    ) -> AsyncGenerator[list[Any]]:
        """The rows of the built statement, read off a server-side cursor a batch at a time."""
        raise NotImplementedError()
