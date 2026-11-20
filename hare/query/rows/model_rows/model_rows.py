from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.dialects.base.constants import CHUNK_SIZE
from hare.dialects.base.results.statement_result import StatementResult
from hare.exceptions import QueryError
from hare.models.instances.dirty_fields import DirtyFields
from hare.native.native_modules import NativeModules
from hare.query.execution.query_execution import QueryExecution
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.rows.model_rows.model_columns import ModelColumns

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher
    from hare.query.rows.native.hydration_layout import HydrationEntry

#: Per ``select_related()`` bucket: its model, how many columns it selects, its table, the model
#: of the instance it hangs off, and the relation path to it (the base model's bucket first).
SelectRelatedBucket = tuple["type[Model]", int, Any, "type[Model]", "Iterable[str | None]"]


class ModelRows:
    """How the rows of a query of model instances are read: each row becomes an instance, with the
    ``select_related()`` instances hung off it and its annotations set on it; then the relations
    asked for are prefetched. The columns of each model are worked out once per result, and each row
    is decoded through its prepared ``ModelResultLayout``.

    A reader follows from the query's shape and the dialect alone - it holds no connection, so the
    one made for a plan is kept in ``StatementPlan.result_reading``. Each query reads its rows
    through that reader on its own connection.

    Args:
        binding: The prepared binding of the queried model.
        types: The type registry of the dialect the rows are read with - it decodes the annotations.
        select_related_buckets: The models the rows hold columns of, in column order - None for the
            queried model's own columns alone.
        decode_plan: The fields of the queried model's columns when the query knows them up front -
            otherwise read off the result's column names.
        decode_plan_is_partial: Whether ``decode_plan`` covers only some of the model's fields.
        decode_plan_key: The compiler's selected-column key. Explicit plans without this key
            are bound independently, preserving their supplied conversion callbacks.
        annotations: The annotation columns, set on each instance by name.
        annotation_fields: The field an annotation's value is decoded through, by name - an
            annotation without one is set as the driver returns it. Only prepared decoders are retained.
        prepared_columns: A fixed positional projection prepared with the statement plan. The
            shared layout contains conversion metadata, never a connection or loaded instances.
        annotation_columns: Public annotation names mapped to their compiled SQL column names.
        source_column_names: Original names in SQL column order when the compiler renamed
            conflicting outputs. These names identify model fields; driver values are still
            read by their actual SQL names or positions.
    """

    native_rows: ClassVar[Any] = NativeModules.rows
    #: Prefetch builds querysets, so its class is imported only when rows need it.
    prefetcher_class: ClassVar[DeferredImport[type[Prefetcher]]] = DeferredImport(
        "hare.query.relation_loading.prefetching.prefetcher", "Prefetcher"
    )

    __slots__ = (
        "_binding",
        "select_related_buckets",
        "decode_plan",
        "decode_plan_is_partial",
        "decode_plan_key",
        "annotations",
        "annotation_column_names",
        "source_column_names",
        "annotation_decoders",
        "prepared_columns",
        "prepared_result",
        "combined_output_names",
    )

    def __init__(
        self,
        binding: ModelBinding,
        types: TypeRegistry,
        *,
        select_related_buckets: Sequence[SelectRelatedBucket] | None = None,
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        decode_plan_key: Sequence[str | None] | None = None,
        annotations: Sequence[str] = (),
        annotation_fields: Mapping[str, Field[Any]] | None = None,
        annotation_columns: Mapping[str, str] | None = None,
        source_column_names: tuple[str | None, ...] = (),
        combined_output_names: Sequence[str] = (),
    ) -> None:
        self._binding = binding
        self.source_column_names = source_column_names
        self.combined_output_names = tuple(combined_output_names)
        self.prepared_columns: list[Any] | None = None
        self.select_related_buckets = select_related_buckets
        self.decode_plan = decode_plan
        self.decode_plan_is_partial = decode_plan_is_partial
        self.decode_plan_key = None if decode_plan_key is None else tuple(decode_plan_key)
        self.annotations = tuple(annotations)
        self.annotation_column_names = (
            self.annotations
            if annotation_columns is None
            else tuple(annotation_columns.get(name, name) for name in self.annotations)
        )
        if annotation_fields:
            python_reader = types.get_python_reader
            self.annotation_decoders: dict[str, Callable[[Any], Any]] = {
                name: python_reader(field) for name, field in annotation_fields.items()
            }
        else:
            self.annotation_decoders = {}

        self.prepared_result: Any = None

    def with_extra_annotations(self, *names: str) -> ModelRows:
        """Copy the reader with additional raw result columns, preserving the cached layout.

        Existing column aliases and decoders remain unchanged. The copy prepares its
        annotation positions from the actual result because the extra columns are not
        part of the original statement's prepared layout.
        """
        reader = copy(self)
        reader.annotations = (*self.annotations, *names)
        reader.annotation_column_names = (*self.annotation_column_names, *names)
        reader.prepared_result = None
        return reader

    def get_result_layout(self, columns: list[Any], annotations: tuple[tuple[str, int | str, Any], ...]) -> Any:
        """Prepare model attachments and annotation readers without execution state."""
        return self.native_rows.ModelResultLayout(columns, annotations)

    def get_columns(self, connection: DatabaseClient, result: StatementResult) -> list[Any]:
        """The columns of each model the rows hold, the queried model first.

        Args:
            connection: The connection the rows are read on.
            result: The fetched rows, at least one, with their column names.

        Returns:
            The columns of each model.
        """
        if self.prepared_columns is not None:
            return self.prepared_columns
        model = self._binding._model
        by_position = connection.features.supports_positional_rows
        decode_plan = self.decode_plan
        column_names = result.column_names if decode_plan is None or not by_position else ()
        buckets = self.select_related_buckets
        # The same columns of the same query shape are laid out the same way - kept per result columns.
        columns_key = (
            model,
            connection.connection_alias,
            connection.dialect,
            connection.native_python_types,
            column_names,
            self.annotations,
            self.annotation_column_names,
            self.source_column_names,
            by_position,
            tuple((bucket[0], bucket[1], tuple(bucket[-1])) for bucket in buckets) if buckets else None,
            self.decode_plan_key,
            self.decode_plan_is_partial,
        )
        columns_bucket = (
            StatementPlans.row_columns.get_bucket(self._binding)
            if decode_plan is None or self.decode_plan_key is not None
            else None
        )
        if columns_bucket is not None:
            kept_columns = columns_bucket.get(columns_key)
            if kept_columns is not None:
                return kept_columns
        if decode_plan is not None:
            keys: tuple[int | str, ...] = (
                tuple(range(len(decode_plan))) if by_position else column_names[: len(decode_plan)]
            )
            columns = [ModelColumns.from_entries(self._binding, decode_plan, keys, self.decode_plan_is_partial)]
        else:
            layout = self._binding.get_hydration_layout(connection)
            source_names = (
                [
                    name if name is not None else column_names[position]
                    for position, name in enumerate(self.source_column_names)
                ]
                if self.source_column_names
                else column_names
            )
            annotation_names = set(self.annotation_column_names)
            positions = [position for position, name in enumerate(column_names) if name not in annotation_names]
            base_column_count = buckets[0][1] if buckets else len(positions)
            base_positions = positions[:base_column_count]
            columns = [
                ModelColumns.build(
                    self._binding,
                    layout,
                    [source_names[position] for position in base_positions],
                    [position if by_position else column_names[position] for position in base_positions],
                    names_are_fields=self.decode_plan_is_partial,
                )
            ]
            offset = base_column_count
            base_path = tuple(buckets[0][-1]) if buckets else ()
            for related_model, column_count, _table, _parent_model, full_path in (buckets or ())[1:]:
                bucket_positions = positions[offset : offset + column_count]
                offset += column_count
                related_binding = self._binding.graph.get_binding(related_model)
                columns.append(
                    ModelColumns.build(
                        related_binding,
                        related_binding.get_hydration_layout(connection),
                        # A joined relation's column is named after its path: "relation.field".
                        [source_names[position].split(".", 1)[1] for position in bucket_positions],
                        [position if by_position else column_names[position] for position in bucket_positions],
                        # Relative to the queried model, whose own path every related path starts with.
                        path=tuple(full_path)[len(base_path) :],  # type: ignore[arg-type]
                        is_related=True,
                        names_are_fields=True,
                    )
                )
        if columns_bucket is not None:
            columns_bucket[columns_key] = columns
        return columns

    def get_annotation_layout(
        self, connection: DatabaseClient, column_names: Sequence[str]
    ) -> tuple[tuple[str, int | str, Any], ...]:
        """The annotation columns in their attribute order, bound to this result's driver keys.

        Args:
            connection: The connection the rows are read on.
            column_names: The result's column names, in order.

        Returns:
            The shared column layout used to assign annotation values.
        """
        by_position = connection.features.supports_positional_rows
        return tuple(
            (name, column_names.index(column_name) if by_position else column_name, self.annotation_decoders.get(name))
            for name, column_name in zip(self.annotations, self.annotation_column_names, strict=True)
        )

    def read_combined(
        self,
        connection: DatabaseClient,
        rows: Iterable[Any],
        column_names: Sequence[str],
        models: Iterable[type[Model]],
        app_column: str,
        model_column: str,
        *,
        context: HareContext | None = None,
    ) -> Iterator[Model]:
        """The instance of each row of a ``union()`` of model querysets - of the model its
        ``app_column``/``model_column`` name.

        Args:
            connection: The connection the rows are read on.
            rows: The rows.
            column_names: The result's column names, in order.
            models: The models the rows can belong to.
            app_column: The column holding the model's app.
            model_column: The column holding the model's class name.
            context: The application context attached to the hydrated instances, if supplied.

        Yields:
            The instances.

        Raises:
            QueryError: A row names none of ``models``.
        """
        return self.get_combined_reader(connection, column_names, models, app_column, model_column, context=context)(
            rows
        )

    def get_combined_reader(
        self,
        connection: DatabaseClient,
        column_names: Sequence[str],
        models: Iterable[type[Model]],
        app_column: str,
        model_column: str,
        *,
        context: HareContext | None = None,
    ) -> Callable[[Iterable[Any]], Iterator[Model]]:
        """Prepare one execution's union-model layouts, reused for every cursor batch."""
        by_position = connection.features.supports_positional_rows
        output_names = self.combined_output_names or column_names
        excluded = {*self.annotations, app_column, model_column}
        positions = [position for position, name in enumerate(output_names) if name not in excluded]
        names = [output_names[position] for position in positions]
        row_keys = [position if by_position else column_names[position] for position in positions]
        columns_by_model: dict[tuple[str | None, str], Any] = {}
        for model in models:
            model_binding = self._binding.graph.get_binding(model)
            model_identity = model_binding.app, model.__name__
            columns_by_model[model_identity] = ModelColumns.build(
                model_binding,
                model_binding.get_hydration_layout(connection),
                names,
                row_keys,
                names_are_fields=bool(self.combined_output_names),
            )
        connection_alias = connection.connection_alias
        annotation_layout = tuple(
            (
                name,
                output_names.index(name) if by_position else column_names[output_names.index(name)],
                self.annotation_decoders.get(name),
            )
            for name in self.annotations
        )
        layouts_by_model = {
            key: self.native_rows.ModelResultLayout((columns,), annotation_layout)
            for key, columns in columns_by_model.items()
        }
        app_index, model_index = output_names.index(app_column), output_names.index(model_column)
        app_key = app_index if by_position else column_names[app_index]
        model_key = model_index if by_position else column_names[model_index]

        def read_rows(rows: Iterable[Any]) -> Iterator[Model]:
            for row in rows:
                layout = layouts_by_model.get((row[app_key], row[model_key]))
                if layout is None:
                    raise QueryError(
                        f"A union row names {row[app_key]}.{row[model_key]}, which is none of the "
                        f"models {sorted(f'{app}.{name}' for app, name in layouts_by_model)} it was built from"
                    )
                yield layout.read_row(row, connection_alias, context, DirtyFields.snapshot_dirty_fields)

        return read_rows

    async def fetch(
        self,
        connection: DatabaseClient,
        sql: str,
        values: list[Any] | None,
        prefetch: PrefetchRequest | None = None,
        *,
        retry_read: bool = False,
        specification: QuerySpecification[Any],
    ) -> Any:
        """Runs a query and reads its rows - the one road of a read of instances, on a plan found by its
        calls or on a query just built.

        Args:
            connection: The connection the query runs on.
            sql: The statement.
            values: Its parameters.
            prefetch: The relations prefetched for the instances.
            retry_read: Whether a lost connection may retry the read.
            specification: The query read - its read checks made (``QueryExecution.check_read_lock()``,
                ``raise_read_error()``) and its single row or list returned. It supplies the
                application context for construction and prefetching.

        Returns:
            The instances - or the one instance of a single-row query.
        """
        context = specification._context
        QueryExecution.check_read_lock(specification, connection)
        try:
            result = await QueryExecution.fetch_rows(connection, sql, values, retry_read=retry_read)
            instances = self.read_all(connection, result, context=context) if result.rows else []
            if not isinstance(instances, list):
                instances = await instances
        except Exception as error:
            QueryExecution.raise_read_error(connection, error)
            raise
        if prefetch is not None and instances:
            await self.prefetcher_class(self._binding, connection, context).prefetch(instances, prefetch)
        return GetExceptions.single_or_list_result(specification, instances)

    def _get_unprepared_result_layout(self, connection: DatabaseClient, result: StatementResult) -> Any:
        columns = self.get_columns(connection, result)
        annotations = self.get_annotation_layout(connection, result.column_names) if self.annotations else ()
        return self.get_result_layout(columns, annotations)

    def read_all(
        self,
        connection: DatabaseClient,
        result: StatementResult,
        *,
        context: HareContext | None = None,
    ) -> list[Model] | Coroutine[Any, Any, list[Model]]:
        """The instances of fetched rows - in chunks, yielding to the event loop between them so
        a large result doesn't block it.

        Args:
            connection: The connection the rows were read on.
            result: The fetched rows, at least one, with their column names.
            context: The application context attached to the hydrated instances, if supplied.

        Returns:
            The instances.
        """
        layout = self.prepared_result or self._get_unprepared_result_layout(connection, result)
        rows = result.rows
        if len(rows) < CHUNK_SIZE:
            return self.read_batch(connection, rows, layout, context=context)
        return self.read_chunks(connection, rows, layout, context=context)

    async def read_chunks(
        self, connection: DatabaseClient, rows: Sequence[Any], layout: Any, *, context: HareContext | None = None
    ) -> list[Model]:
        """Yield between bounded hydration batches, preserving cancellation points."""
        instances: list[Model] = []
        for start in range(0, len(rows), CHUNK_SIZE):
            stop = min(start + CHUNK_SIZE, len(rows))
            instances.extend(self.read_batch(connection, rows, layout, start=start, stop=stop, context=context))
            if stop - start == CHUNK_SIZE:
                await asyncio.sleep(0)
        return instances

    async def stream_batches(
        self,
        connection: DatabaseClient,
        sql: str,
        values: list[Any] | None,
        chunk_size: int = 0,
        *,
        context: HareContext | None = None,
    ) -> AsyncGenerator[list[Model]]:
        """Runs a query off a server-side cursor, yielding the instances a batch of rows at a time.

        Args:
            connection: The connection the query runs on.
            sql: The statement.
            values: Its parameters.
            chunk_size: How many rows to fetch per round trip, where the driver honors it.
            context: The application context attached to the hydrated instances, if supplied.

        Yields:
            The instances.
        """
        layout = self.prepared_result
        async with contextlib.aclosing(
            connection._stream_built_batches(sql, values, chunk_size=chunk_size)
        ) as batches:
            async for rows in batches:
                if not rows:
                    yield []
                    continue
                if layout is None:
                    layout = self._get_unprepared_result_layout(connection, StatementResult(len(rows), rows))
                yield self.read_batch(connection, rows, layout, context=context)

    def read_batch(
        self,
        connection: DatabaseClient,
        rows: Sequence[Any],
        layout: Any,
        *,
        start: int = 0,
        stop: int | None = None,
        context: HareContext | None = None,
    ) -> list[Model]:
        """The instances of a batch, decoded through the selected models' column layouts.

        Args:
            connection: The connection the rows were read on - the alias the instances keep.
            rows: The fetched rows.
            layout: The prepared model and annotation layout, shared by the result's batches.
            start: The first row to decode, inclusive.
            stop: The end of the requested row slice, exclusive. None reads through the last row.
            context: The application context attached to the hydrated instances, if supplied.

        Returns:
            The instances.
        """
        return layout.read_batch(
            rows,
            start,
            len(rows) if stop is None else stop,
            connection.connection_alias,
            context,
            DirtyFields.snapshot_dirty_fields,
        )
