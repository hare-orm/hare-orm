from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
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
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model
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
    is decoded through the same ``RowLayout`` used for values results.

    A reader follows from the query's shape and the dialect alone - it holds no connection, so the
    one made for a plan is kept with it (``AwaitableQuery._get_call_plan_reading()``) and every
    query of the plan reads its rows through it, on its own connection.

    Args:
        model: The queried model.
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
            annotation without one is set as the driver returns it.
        prepared_columns: A fixed positional projection prepared with the statement plan. The
            shared layout contains conversion metadata, never a connection or loaded instances.
    """

    native_rows: ClassVar[Any] = NativeModules.rows
    #: Prefetch builds querysets, so its class is imported only when rows need it.
    prefetcher_class: ClassVar[DeferredImport[type[Prefetcher]]] = DeferredImport(
        "hare.query.relation_loading.prefetching.prefetcher", "Prefetcher"
    )

    __slots__ = (
        "model",
        "select_related_buckets",
        "decode_plan",
        "decode_plan_is_partial",
        "decode_plan_key",
        "annotations",
        "annotation_fields",
        "annotation_decoders",
        "prepared_columns",
        "prepared_result",
    )

    def __init__(
        self,
        model: type[Model],
        types: TypeRegistry,
        *,
        select_related_buckets: Sequence[SelectRelatedBucket] | None = None,
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        decode_plan_key: Sequence[str | None] | None = None,
        annotations: Sequence[str] = (),
        annotation_fields: Mapping[str, Field[Any]] | None = None,
        prepared_columns: list[ModelColumns] | None = None,
        prepared_column_names: tuple[str, ...] = (),
    ) -> None:
        self.model = model
        self.prepared_columns = prepared_columns
        self.select_related_buckets = select_related_buckets
        self.decode_plan = decode_plan
        self.decode_plan_is_partial = decode_plan_is_partial
        self.decode_plan_key = None if decode_plan_key is None else tuple(decode_plan_key)
        self.annotations = tuple(annotations)
        if annotation_fields:
            self.annotation_fields = annotation_fields
            python_reader = types.get_python_reader
            self.annotation_decoders: dict[str, Callable[[Any], Any]] = {
                name: python_reader(field) for name, field in annotation_fields.items()
            }
        else:
            self.annotation_fields = {}
            self.annotation_decoders = {}

        self.prepared_result = (
            self.get_result_layout(
                prepared_columns,
                tuple(
                    (name, prepared_column_names.index(name), self.annotation_decoders.get(name))
                    for name in self.annotations
                ),
            )
            if prepared_columns is not None and prepared_column_names
            else None
        )

    def get_result_layout(
        self, columns: list[ModelColumns], annotations: tuple[tuple[str, int | str, Any], ...]
    ) -> Any:
        """Prepare model attachments and annotation readers without execution state."""
        return self.native_rows.ModelResultLayout(columns, annotations)

    def get_columns(self, connection: DatabaseClient, result: StatementResult) -> list[ModelColumns]:
        """The columns of each model the rows hold, the queried model first.

        Args:
            connection: The connection the rows are read on.
            result: The fetched rows, at least one, with their column names.

        Returns:
            The columns of each model.
        """
        if self.prepared_columns is not None:
            return self.prepared_columns
        model = self.model
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
            by_position,
            tuple((bucket[0], bucket[1], tuple(bucket[-1])) for bucket in buckets) if buckets else None,
            self.decode_plan_key,
            self.decode_plan_is_partial,
        )
        columns_bucket = (
            StatementPlans.row_columns.get_bucket(model)
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
            columns = [ModelColumns(model, decode_plan, keys, self.decode_plan_is_partial)]
        else:
            layout = model._meta.get_hydration_layout(connection)
            annotation_names = set(self.annotations)
            positions = [position for position, name in enumerate(column_names) if name not in annotation_names]
            base_column_count = buckets[0][1] if buckets else len(positions)
            base_positions = positions[:base_column_count]
            columns = [
                ModelColumns.build(
                    model,
                    layout,
                    [column_names[position] for position in base_positions],
                    [position if by_position else column_names[position] for position in base_positions],
                )
            ]
            offset = base_column_count
            base_path = tuple(buckets[0][-1]) if buckets else ()
            for related_model, column_count, _table, _parent_model, full_path in (buckets or ())[1:]:
                bucket_positions = positions[offset : offset + column_count]
                offset += column_count
                columns.append(
                    ModelColumns.build(
                        related_model,
                        related_model._meta.get_hydration_layout(connection),
                        # A joined relation's column is named after its path: "relation.field".
                        [column_names[position].split(".", 1)[1] for position in bucket_positions],
                        [position if by_position else column_names[position] for position in bucket_positions],
                        # Relative to the queried model, whose own path every related path starts with.
                        path=tuple(full_path)[len(base_path) :],  # type: ignore[arg-type]
                        is_related=True,
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
            (name, column_names.index(name) if by_position else name, self.annotation_decoders.get(name))
            for name in self.annotations
        )

    def read_combined(
        self,
        connection: DatabaseClient,
        rows: Iterable[Any],
        column_names: Sequence[str],
        models: Iterable[type[Model]],
        app_column: str,
        model_column: str,
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

        Yields:
            The instances.

        Raises:
            QueryError: A row names none of ``models``.
        """
        by_position = connection.features.supports_positional_rows
        excluded = {*self.annotations, app_column, model_column}
        positions = [position for position, name in enumerate(column_names) if name not in excluded]
        names = [column_names[position] for position in positions]
        row_keys = [position if by_position else column_names[position] for position in positions]
        columns_by_model = {
            (model._meta.app, model.__name__): ModelColumns.build(
                model, model._meta.get_hydration_layout(connection), names, row_keys
            )
            for model in models
        }
        connection_alias = connection.connection_alias
        annotation_layout = self.get_annotation_layout(connection, column_names)
        layouts_by_model = {
            key: self.native_rows.ModelResultLayout((columns,), annotation_layout)
            for key, columns in columns_by_model.items()
        }
        app_key = column_names.index(app_column) if by_position else app_column
        model_key = column_names.index(model_column) if by_position else model_column
        for row in rows:
            columns = columns_by_model.get((row[app_key], row[model_key]))
            if columns is None:
                raise QueryError(
                    f"A union row names {row[app_key]}.{row[model_key]}, which is none of the "
                    f"models {sorted(f'{app}.{name}' for app, name in columns_by_model)} it was built from"
                )
            yield layouts_by_model[(row[app_key], row[model_key])].read_row(
                row, connection_alias, DirtyFields.snapshot_dirty_fields
            )

    async def fetch(
        self,
        connection: DatabaseClient,
        sql: str,
        values: list[Any] | None,
        prefetch: PrefetchRequest | None = None,
        *,
        retry_read: bool = False,
        specification: QuerySpecification[Any] | None = None,
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
                ``raise_read_error()``) and its single row or list returned; None for the rows of a
                write returning them.

        Returns:
            The instances - or the one instance of a single-row query.
        """
        if specification is not None:
            QueryExecution.check_read_lock(specification, connection)
        try:
            # Model rows - a SELECT, or a write RETURNING them; read by position where the driver can.
            result = await connection.execute(
                sql,
                values,
                returns_rows=True,
                rows_by_position=connection.features.supports_positional_rows,
                retry_read=retry_read,
            )
            instances = self.read_all(connection, result) if result.rows else []
            if not isinstance(instances, list):
                instances = await instances
        except Exception as error:
            if specification is not None:
                QueryExecution.raise_read_error(connection, error)
            raise
        if prefetch is not None and instances:
            await self.prefetcher_class(self.model, connection).prefetch(instances, prefetch)
        return instances if specification is None else GetExceptions.single_or_list_result(specification, instances)

    def read_all(
        self, connection: DatabaseClient, result: StatementResult
    ) -> list[Model] | Coroutine[Any, Any, list[Model]]:
        """The instances of fetched rows - in chunks, yielding to the event loop between them so
        a large result doesn't block it.

        Args:
            connection: The connection the rows were read on.
            result: The fetched rows, at least one, with their column names.

        Returns:
            The instances.
        """
        layout = self.prepared_result or self.get_result_layout(
            self.get_columns(connection, result),
            self.get_annotation_layout(connection, result.column_names) if self.annotations else (),
        )
        if len(result.rows) < CHUNK_SIZE:
            return self.read_batch(connection, result, layout)
        return self.read_chunks(connection, result, layout)

    async def read_chunks(self, connection: DatabaseClient, result: StatementResult, layout: Any) -> list[Model]:
        """Yield between bounded hydration batches, preserving cancellation points."""
        instances: list[Model] = []
        for start in range(0, len(result.rows), CHUNK_SIZE):
            stop = min(start + CHUNK_SIZE, len(result.rows))
            instances.extend(self.read_batch(connection, result, layout, start=start, stop=stop))
            if stop - start == CHUNK_SIZE:
                await asyncio.sleep(0)
        return instances

    async def stream_batches(
        self, connection: DatabaseClient, sql: str, values: list[Any] | None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Model]]:
        """Runs a query off a server-side cursor, yielding the instances a batch of rows at a time.

        Args:
            connection: The connection the query runs on.
            sql: The statement.
            values: Its parameters.
            chunk_size: How many rows to fetch per round trip, where the driver honors it.

        Yields:
            The instances.
        """
        layout = self.prepared_result
        async with contextlib.aclosing(connection.stream_batches(sql, values, chunk_size=chunk_size)) as batches:
            async for rows in batches:
                if not rows:
                    yield []
                    continue
                # A batch of the driver's rows, read by name too.
                batch = StatementResult(len(rows), rows)
                if layout is None:
                    layout = self.get_result_layout(
                        self.get_columns(connection, batch),
                        self.get_annotation_layout(connection, batch.column_names) if self.annotations else (),
                    )
                yield self.read_batch(connection, batch, layout)

    def read_batch(
        self,
        connection: DatabaseClient,
        result: StatementResult,
        layout: Any,
        *,
        start: int = 0,
        stop: int | None = None,
    ) -> list[Model]:
        """The instances of a batch, decoded through the selected models' column layouts.

        Args:
            connection: The connection the rows were read on - the alias the instances keep.
            result: The rows, at least one, with their column names.
            layout: The prepared model and annotation layout, shared by the result's batches.

        Returns:
            The instances.
        """
        return layout.read_batch(
            result.rows,
            start,
            len(result.rows) if stop is None else stop,
            connection.connection_alias,
            DirtyFields.snapshot_dirty_fields,
        )
