from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import CHUNK_SIZE
from hare.dialects.base.results.statement_result import StatementResult
from hare.exceptions import QueryError
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.model_rows.model_columns import ModelColumns
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
    from hare.query.rows.native.hydration_layout import HydrationEntry

#: Per ``select_related()`` bucket: its model, how many columns it selects, its table, the model
#: of the instance it hangs off, and the relation path to it (the base model's bucket first).
SelectRelatedBucket = tuple["type[Model]", int, Any, "type[Model]", "Iterable[str | None]"]


class ModelRows:
    """How the rows of a query of model instances are read: each row becomes an instance, with the
    ``select_related()`` instances hung off it and its annotations set on it; then the relations
    asked for are prefetched. The columns of each model are worked out once per result, and each row
    is decoded through the same ``RowLayout`` used for values results.

    Args:
        model: The queried model.
        connection: The connection the rows are read on.
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
    """

    __slots__ = (
        "model",
        "connection",
        "select_related_buckets",
        "decode_plan",
        "decode_plan_is_partial",
        "decode_plan_key",
        "annotations",
        "annotation_fields",
        "annotation_decoders",
    )

    def __init__(
        self,
        model: type[Model],
        connection: DatabaseClient,
        *,
        select_related_buckets: Sequence[SelectRelatedBucket] | None = None,
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        decode_plan_key: Sequence[str | None] | None = None,
        annotations: Sequence[str] = (),
        annotation_fields: dict[str, Field[Any]] | None = None,
    ) -> None:
        self.model = model
        self.connection = connection
        self.select_related_buckets = select_related_buckets
        self.decode_plan = decode_plan
        self.decode_plan_is_partial = decode_plan_is_partial
        self.decode_plan_key = tuple(decode_plan_key) if decode_plan_key is not None else None
        self.annotations = tuple(annotations)
        self.annotation_fields = annotation_fields or {}
        python_reader = connection.dialect.types.get_python_reader
        self.annotation_decoders: dict[str, Callable[[Any], Any]] = {
            name: python_reader(field) for name, field in self.annotation_fields.items()
        }

    def get_columns(self, result: StatementResult) -> list[ModelColumns]:
        """The columns of each model the rows hold, the queried model first.

        Args:
            result: The fetched rows, at least one, with their column names.

        Returns:
            The columns of each model.
        """
        model = self.model
        connection = self.connection
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
            StatementPlans.row_columns.get_model_bucket(model)
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
                        path=tuple(cast("Iterable[str]", full_path))[len(base_path) :],
                        is_related=True,
                    )
                )
        if columns_bucket is not None:
            columns_bucket[columns_key] = columns
        return columns

    def read(self, result: StatementResult, columns: list[ModelColumns]) -> Iterator[Model]:
        """The instance of each row, through its declared column layout.

        Args:
            result: The fetched rows, with their column names.
            columns: The columns of each model the rows hold.

        Yields:
            The queried model's instance of each row.
        """
        connection_alias = self.connection.connection_alias
        base_hydrate = columns[0].hydrate
        related = [
            (
                model_columns.hydrate,
                model_columns.path[:-1],
                f"_{model_columns.path[-1]}",
                model_columns.path,
            )
            for model_columns in columns[1:]
        ]
        annotation_layout = self.get_annotation_layout(result.column_names) if self.annotations else None
        for row in result.rows:
            instance = base_hydrate(row, connection_alias)
            if related:
                instances_by_path: dict[tuple[str, ...], Any] = {(): instance}
                for hydrate, parent_path, cache_attribute, path in related:
                    related_instance = hydrate(row, connection_alias)
                    parent = instances_by_path.get(parent_path)
                    if parent is not None:
                        object.__setattr__(parent, cache_attribute, related_instance)
                    if related_instance is not None:
                        instances_by_path[path] = related_instance
            if annotation_layout is not None:
                annotation_layout.assign(instance, row, self.annotations)
            yield instance

    def get_annotation_layout(self, column_names: Sequence[str]) -> RowLayout:
        """The annotation columns in their attribute order, bound to this result's driver keys.

        Args:
            column_names: The result's column names, in order.

        Returns:
            The shared column layout used to assign annotation values.
        """
        by_position = self.connection.features.supports_positional_rows
        return RowLayout(
            tuple(
                RowColumn(column_names.index(name) if by_position else name, self.annotation_decoders.get(name))
                for name in self.annotations
            )
        )

    def read_combined(
        self,
        rows: Iterable[Any],
        column_names: Sequence[str],
        models: Iterable[type[Model]],
        app_column: str,
        model_column: str,
    ) -> Iterator[Model]:
        """The instance of each row of a ``union()`` of model querysets - of the model its
        ``app_column``/``model_column`` name.

        Args:
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
        connection = self.connection
        by_position = connection.features.supports_positional_rows
        excluded = {*self.annotations, app_column, model_column}
        positions = [position for position, name in enumerate(column_names) if name not in excluded]
        names = [column_names[position] for position in positions]
        row_keys = [position if by_position else column_names[position] for position in positions]
        hydrate_by_model = {
            (model._meta.app, model.__name__): ModelColumns.build(
                model, model._meta.get_hydration_layout(connection), names, row_keys
            ).hydrate
            for model in models
        }
        connection_alias = connection.connection_alias
        annotation_layout = self.get_annotation_layout(column_names) if self.annotations else None
        app_key = column_names.index(app_column) if by_position else app_column
        model_key = column_names.index(model_column) if by_position else model_column
        for row in rows:
            hydrate = hydrate_by_model.get((row[app_key], row[model_key]))
            if hydrate is None:
                raise QueryError(
                    f"A union row names {row[app_key]}.{row[model_key]}, which is none of the "
                    f"models {sorted(f'{app}.{name}' for app, name in hydrate_by_model)} it was built from"
                )
            instance = hydrate(row, connection_alias)
            if annotation_layout is not None:
                annotation_layout.assign(instance, row, self.annotations)
            yield instance

    async def fetch(
        self, sql: str, values: list[Any] | None, prefetch: PrefetchRequest | None = None, *, retry_read: bool = False
    ) -> list[Model]:
        """Runs a query and reads its rows.

        Args:
            sql: The statement.
            values: Its parameters.
            prefetch: The relations prefetched for the instances.

        Returns:
            The instances.
        """
        connection = self.connection
        # Model rows - a SELECT, or a write RETURNING them; read by position where the driver can.
        result = await connection.execute(
            sql,
            values,
            returns_rows=True,
            rows_by_position=connection.features.supports_positional_rows,
            retry_read=retry_read,
        )
        if not result.rows:
            return []
        instances = await self.read_all(result)
        if prefetch is not None:
            # Deferred import: the prefetch module builds querysets, which import this module.
            from hare.query.relation_loading.prefetching.prefetcher import Prefetcher

            await Prefetcher(self.model, self.connection).prefetch(instances, prefetch)
        return instances

    async def read_all(self, result: StatementResult) -> list[Model]:
        """The instances of fetched rows - in chunks, yielding to the event loop between them so
        a large result doesn't block it.

        Args:
            result: The fetched rows, at least one, with their column names.

        Returns:
            The instances.
        """
        columns = self.get_columns(result)
        hydrated_rows = self.read(result, columns)
        instances: list[Model] = []
        while True:
            chunk = list(itertools.islice(hydrated_rows, CHUNK_SIZE))
            instances.extend(chunk)
            if len(chunk) < CHUNK_SIZE:
                break
            await asyncio.sleep(0)
        return instances

    async def stream_batches(
        self, sql: str, values: list[Any] | None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Model]]:
        """Runs a query off a server-side cursor, yielding the instances a batch of rows at a time.

        Args:
            sql: The statement.
            values: Its parameters.
            chunk_size: How many rows to fetch per round trip, where the driver honors it.

        Yields:
            The instances.
        """
        columns: list[ModelColumns] | None = None
        connection = self.connection
        async with contextlib.aclosing(connection.stream_batches(sql, values, chunk_size=chunk_size)) as batches:
            async for rows in batches:
                if not rows:
                    yield []
                    continue
                # A batch of the driver's rows, read by name too.
                batch = StatementResult(len(rows), rows)
                if columns is None:
                    columns = self.get_columns(batch)
                yield self.read_batch(batch, columns)

    def read_batch(self, result: StatementResult, columns: list[ModelColumns]) -> list[Model]:
        """The instances of a batch, decoded through the selected models' column layouts.

        Args:
            result: The rows, at least one, with their column names.
            columns: The columns of each model the rows hold.

        Returns:
            The instances.
        """
        return list(self.read(result, columns))
