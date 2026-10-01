from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import CHUNK_SIZE
from hare.exceptions import QueryError
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.model_columns import ModelColumns
from hare.query.rows.value_field import ValueField
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.fields.base.field import Field
    from hare.models import Model
    from hare.query.relation_loading.prefetch_request import PrefetchRequest
    from hare.query.rows.hydration_layout import HydrationEntry

#: Per ``select_related()`` bucket: its model, how many columns it selects, its table, the model
#: of the instance it hangs off, and the relation path to it (the base model's bucket first).
SelectRelatedBucket = tuple["type[Model]", int, Any, "type[Model]", "Iterable[str | None]"]


class ModelRows:
    """How the rows of a query of model instances are read: each row becomes an instance, with the
    ``select_related()`` instances hung off it and its annotations set on it; then the relations
    asked for are prefetched. The columns of each model are worked out once per result, and each row
    goes through the function compiled for them - or every row in one call of the ``rust.native.rows``
    module.

    Args:
        model: The queried model.
        db: The connection the rows are read on.
        select_related_buckets: The models the rows hold columns of, in column order - None for the
            queried model's own columns alone.
        decode_plan: The fields of the queried model's columns when the query knows them up front -
            otherwise read off the result's column names.
        decode_plan_is_partial: Whether ``decode_plan`` covers only some of the model's fields.
        annotations: The annotation columns, set on each instance by name.
        annotation_fields: The field an annotation's value is decoded through, by name - an
            annotation without one is set as the driver returns it.
    """

    __slots__ = (
        "model",
        "db",
        "select_related_buckets",
        "decode_plan",
        "decode_plan_is_partial",
        "annotations",
        "annotation_fields",
        "annotation_decoders",
    )

    def __init__(
        self,
        model: type[Model],
        db: DatabaseClient,
        *,
        select_related_buckets: Sequence[SelectRelatedBucket] | None = None,
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        annotations: Sequence[str] = (),
        annotation_fields: dict[str, Field[Any]] | None = None,
    ) -> None:
        self.model = model
        self.db = db
        self.select_related_buckets = select_related_buckets
        self.decode_plan = decode_plan
        self.decode_plan_is_partial = decode_plan_is_partial
        self.annotations = tuple(annotations)
        self.annotation_fields = annotation_fields or {}
        python_reader = db.dialect.types.get_python_reader
        self.annotation_decoders: dict[str, Callable[[Any], Any]] = {
            name: python_reader(field) for name, field in self.annotation_fields.items()
        }

    def get_columns(self, first_row: Any) -> list[ModelColumns]:
        """The columns of each model the rows hold, the queried model first.

        Args:
            first_row: The first row - its keys are the result's column names, in order.

        Returns:
            The columns of each model.
        """
        model = self.model
        db = self.db
        by_position = db.features.supports_positional_rows
        decode_plan = self.decode_plan
        if decode_plan is not None:
            keys: tuple[int | str, ...] = (
                tuple(range(len(decode_plan))) if by_position else tuple(list(first_row.keys())[: len(decode_plan)])
            )
            return [ModelColumns(model, decode_plan, keys, self.decode_plan_is_partial)]
        column_names = list(first_row.keys())
        layout = model._meta.get_hydration_layout(db)
        annotation_names = set(self.annotations)
        positions = [position for position, name in enumerate(column_names) if name not in annotation_names]
        buckets = self.select_related_buckets
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
        for related_model, column_count, *_rest, full_path in (buckets or ())[1:]:
            bucket_positions = positions[offset : offset + column_count]
            offset += column_count
            columns.append(
                ModelColumns.build(
                    related_model,
                    related_model._meta.get_hydration_layout(db),
                    # A joined relation's column is named after its path: "relation.field".
                    [column_names[position].split(".", 1)[1] for position in bucket_positions],
                    [position if by_position else column_names[position] for position in bucket_positions],
                    # Relative to the queried model, whose own path every related path starts with.
                    path=tuple(cast("Iterable[str]", full_path))[len(base_path) :],
                    is_related=True,
                )
            )
        return columns

    def read(self, rows: Iterable[Any], columns: list[ModelColumns]) -> Iterator[Model]:
        """The instance of each row, through the compiled functions.

        Args:
            rows: The rows.
            columns: The columns of each model the rows hold.

        Yields:
            The queried model's instance of each row.
        """
        use_tz = Timezone.get_use_tz()
        tz_ctx = (use_tz, Timezone.default() if use_tz else None)
        connection_name = self.db.connection_name
        base_hydrate = columns[0].get_hydrate_function()
        related = [
            (
                model_columns.get_hydrate_function(),
                model_columns.path[:-1],
                f"_{model_columns.path[-1]}",
                model_columns.path,
            )
            for model_columns in columns[1:]
        ]
        annotations = [(name, self.annotation_decoders.get(name)) for name in self.annotations]
        for row in rows:
            instance = base_hydrate(row, tz_ctx, connection_name)
            if related:
                instances_by_path: dict[tuple[str, ...], Any] = {(): instance}
                for hydrate, parent_path, cache_attribute, path in related:
                    related_instance = hydrate(row, tz_ctx, connection_name)
                    parent = instances_by_path.get(parent_path)
                    if parent is not None:
                        object.__setattr__(parent, cache_attribute, related_instance)
                    if related_instance is not None:
                        instances_by_path[path] = related_instance
            for name, decoder in annotations:
                raw_value = row[name]
                object.__setattr__(instance, name, raw_value if decoder is None else decoder(raw_value))
            yield instance

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
        db = self.db
        by_position = db.features.supports_positional_rows
        excluded = {*self.annotations, app_column, model_column}
        positions = [position for position, name in enumerate(column_names) if name not in excluded]
        names = [column_names[position] for position in positions]
        row_keys = [position if by_position else column_names[position] for position in positions]
        hydrate_by_model = {
            (model._meta.app, model.__name__): ModelColumns.build(
                model, model._meta.get_hydration_layout(db), names, row_keys
            ).get_hydrate_function()
            for model in models
        }
        use_tz = Timezone.get_use_tz()
        tz_ctx = (use_tz, Timezone.default() if use_tz else None)
        connection_name = db.connection_name
        annotations = [(name, self.annotation_decoders.get(name)) for name in self.annotations]
        for row in rows:
            hydrate = hydrate_by_model.get((row[app_column], row[model_column]))
            if hydrate is None:
                raise QueryError(
                    f"A union row names {row[app_column]}.{row[model_column]}, which is none of the "
                    f"models {sorted(f'{app}.{name}' for app, name in hydrate_by_model)} it was built from"
                )
            instance = hydrate(row, tz_ctx, connection_name)
            for name, decoder in annotations:
                raw_value = row[name]
                object.__setattr__(instance, name, raw_value if decoder is None else decoder(raw_value))
            yield instance

    def read_combined_with_accelerator(
        self,
        rows: list[Any],
        column_names: Sequence[str],
        models: Iterable[type[Model]],
        app_column: str,
        model_column: str,
    ) -> list[Model]:
        """``read_combined()`` of positional rows in one ``rust.native.rows`` call.

        Args:
            rows: The rows, at least one.
            column_names: The result's column names, in order.
            models: The models the rows can belong to.
            app_column: The column holding the model's app.
            model_column: The column holding the model's class name.

        Returns:
            The instances.

        Raises:
            QueryError: A row names none of ``models``.
        """
        db = self.db
        types = db.dialect.types
        excluded = {*self.annotations, app_column, model_column}
        positions = [position for position, name in enumerate(column_names) if name not in excluded]
        names = [column_names[position] for position in positions]
        model_indexes: dict[tuple[str | None, str], int] = {}
        model_readers: list[tuple[Any, list[int]]] = []
        for model in models:
            columns = ModelColumns.build(model, model._meta.get_hydration_layout(db), names, positions)
            reader = HydrateAccelerator.get_model_reader(
                model, columns.entries, columns.is_partial, types, Timezone.get_aware_zone_name()
            )
            model_indexes[(model._meta.app, model.__name__)] = len(model_readers)
            model_readers.append((reader, cast("list[int]", list(columns.keys))))
        try:
            instances: list[Model] = HydrateAccelerator.module.read_combined_rows(
                rows,
                model_indexes,
                model_readers,
                column_names.index(app_column),
                column_names.index(model_column),
                db.connection_name,
            )
        except KeyError as error:
            row_app, row_model = error.args[0]
            raise QueryError(
                f"A union row names {row_app}.{row_model}, which is none of the "
                f"models {sorted(f'{app}.{name}' for app, name in model_indexes)} it was built from"
            ) from None
        if self.annotations:
            value_fields = tuple(
                ValueField(None, name, field := self.annotation_fields.get(name), is_native=field is None)
                for name in self.annotations
            )
            HydrateAccelerator.get_values_reader(self.model, value_fields, db).set_attributes(
                instances, rows, [column_names.index(name) for name in self.annotations]
            )
        return instances

    def read_with_accelerator(self, rows: Sequence[Any], columns: list[ModelColumns]) -> list[Model] | None:
        """Every instance in one ``rust.native.rows`` call per model.

        Args:
            rows: The rows, at least one, read by position.
            columns: The columns of each model the rows hold.

        Returns:
            The queried model's instances, or None when a model's columns don't sit next to each
            other.
        """
        connection_name = self.db.connection_name
        types = self.db.dialect.types
        hydrated: list[list[Any]] = []
        for model_columns in columns:
            instances = model_columns.hydrate_with_accelerator(rows, connection_name, types)
            if instances is None:
                return None
            hydrated.append(instances)
        base_instances = hydrated[0]
        if len(columns) > 1:
            instances_by_path: dict[tuple[str, ...], list[Any]] = {(): base_instances}
            for model_columns, instances in zip(columns[1:], hydrated[1:], strict=True):
                path = model_columns.path
                cache_attribute = f"_{path[-1]}"
                for parent, related_instance in zip(instances_by_path[path[:-1]], instances, strict=True):
                    if parent is not None:
                        object.__setattr__(parent, cache_attribute, related_instance)
                instances_by_path[path] = instances
        if self.annotations:
            column_names = list(rows[0].keys())
            value_fields = tuple(
                ValueField(None, name, field := self.annotation_fields.get(name), is_native=field is None)
                for name in self.annotations
            )
            reader = HydrateAccelerator.get_values_reader(self.model, value_fields, self.db)
            reader.set_attributes(
                base_instances,
                rows if type(rows) is list else list(rows),
                [column_names.index(name) for name in self.annotations],
            )
        return cast("list[Model]", base_instances)

    async def fetch(self, sql: str, values: list[Any] | None, prefetch: PrefetchRequest | None = None) -> list[Model]:
        """Runs a query and reads its rows.

        Args:
            sql: The statement.
            values: Its parameters.
            prefetch: The relations prefetched for the instances.

        Returns:
            The instances.
        """
        # Model rows - a SELECT, or a write RETURNING them.
        _, rows = await self.db.execute(sql, values, returns_rows=True)
        if not rows:
            return []
        instances = await self.read_all(rows)
        if prefetch is not None:
            # Deferred import: the prefetch module builds querysets, which import this module.
            from hare.query.relation_loading.prefetcher import Prefetcher

            await Prefetcher(self.model, self.db).prefetch(instances, prefetch)
        return instances

    async def read_all(self, rows: Sequence[Any]) -> list[Model]:
        """The instances of fetched rows - in chunks, yielding to the event loop between them so
        a large result doesn't block it.

        Args:
            rows: The rows, at least one.

        Returns:
            The instances.
        """
        columns = self.get_columns(rows[0])
        accelerator_error: Exception | None = None
        if HydrateAccelerator.module is not None and self.db.features.supports_positional_rows:
            try:
                accelerated = self.read_with_accelerator(rows, columns)
            except (TypeError, AttributeError) as error:
                # A stale build, possibly - the pure-Python path below settles it.
                accelerator_error = error
            else:
                if accelerated is not None:
                    return accelerated
        hydrated_rows = self.read(rows, columns)
        instances: list[Model] = []
        while True:
            chunk = list(itertools.islice(hydrated_rows, CHUNK_SIZE))
            instances.extend(chunk)
            if len(chunk) < CHUNK_SIZE:
                break
            await asyncio.sleep(0)
        if accelerator_error is not None:
            HydrateAccelerator.disable(accelerator_error)
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
        db = cast("TransactionClient", self.db)
        async with contextlib.aclosing(db.stream_batches(sql, values, chunk_size=chunk_size)) as batches:
            async for batch in batches:
                rows = batch if type(batch) is list else list(batch)
                if columns is None:
                    columns = self.get_columns(rows[0])
                yield self.read_batch(rows, columns)

    def read_batch(self, rows: list[Any], columns: list[ModelColumns]) -> list[Model]:
        """The instances of a batch of rows - in one ``rust.native.rows`` call per model where the
        rows are read by position.

        Args:
            rows: The rows, at least one.
            columns: The columns of each model the rows hold.

        Returns:
            The instances.
        """
        if HydrateAccelerator.module is not None and self.db.features.supports_positional_rows:
            accelerated = HydrateAccelerator.run_or_fall_back(
                lambda: self.read_with_accelerator(rows, columns), lambda: list(self.read(rows, columns))
            )
            if accelerated is not None:
                return accelerated
        return list(self.read(rows, columns))
