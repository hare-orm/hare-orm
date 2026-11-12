from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence, Sized
from itertools import batched
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.core.caching.cache import Cache
from hare.exceptions import (
    QueryError,
)
from hare.native.native_modules import NativeModules
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.time.zone_settings import ZoneSettings

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkWriteBatches:
    """How bulk writes split their objects into statements and serialize them."""

    SERIALIZE_INSTANCES_CACHE: ClassVar[Cache[tuple[tuple[str, Callable[..., Any]], ...]]] = Cache(
        holds_sql=False,
    )

    @staticmethod
    def get_batches(instances: Iterable[Any], batch_size: int | None = None) -> Iterable[Iterable[Any]]:
        """Batches of at most ``batch_size`` items - the whole iterable as one batch for None.

        Args:
            instances: The items.
            batch_size: The most items per batch, or None for no limit.

        Raises:
            QueryError: ``batch_size`` is 0 or negative.
        """
        if batch_size is None:
            yield instances
            return
        if batch_size <= 0:
            raise QueryError(
                f"batch_size must be a positive integer, got {ValueDescriptions.get_shown_value(batch_size)}"
            )
        # batched() allocates a whole batch up front - a batch larger than the items is all of them.
        item_count = len(instances) if isinstance(instances, Sized) else sys.maxsize
        yield from batched(instances, min(batch_size, max(item_count, 1)))

    @staticmethod
    def get_values_rows_sql(
        model: type[Model], placeholder_templates: tuple[str, ...], first_index: int, row_count: int
    ) -> str:
        """The rows of a ``VALUES`` list of bound parameters, ``(p1, p2), (p3, p4)``, made once per
        shape and kept with the model's plans.

        Args:
            model: The model written.
            placeholder_templates: Each column's placeholder, formatted with the parameter's index.
            first_index: The index of the first parameter.
            row_count: How many rows.

        Returns:
            The SQL.
        """
        key = (placeholder_templates, first_index, row_count)
        values_rows_sql = StatementPlans.values_rows_sql
        sql = values_rows_sql.get_bucket(model).get(key)
        if sql is None:
            sql = values_rows_sql.get_bucket(model)[key] = BulkWriteBatches.build_values_rows_sql(*key)
        return sql

    @staticmethod
    def build_values_rows_sql(placeholder_templates: tuple[str, ...], first_index: int, row_count: int) -> str:
        """The rows of a ``VALUES`` list of bound parameters - see ``get_values_rows_sql()``."""
        column_count = len(placeholder_templates)
        return ", ".join(
            "("
            + ", ".join(
                template.format(first_index + row_index * column_count + column_index)
                for column_index, template in enumerate(placeholder_templates)
            )
            + ")"
            for row_index in range(row_count)
        )

    @staticmethod
    def check_batch_size(batch_size: int | None, method_name: str) -> None:
        """Refuses a batch size that is not None or a positive int.

        Args:
            batch_size: The caller's batch_size.
            method_name: The bulk method, for the message.

        Raises:
            QueryError: ``batch_size`` is no int, a bool, 0 or negative.
        """
        if batch_size is not None and (
            isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0
        ):
            raise QueryError(
                f"{method_name}(): batch_size must be a positive integer, got "
                f"{ValueDescriptions.get_shown_value(batch_size)}"
            )

    @staticmethod
    def get_bind_parameter_safe_batch_size(requested: int | None, column_count: int, max_bind_parameters: int) -> int:
        """The number of rows one multi-row statement can bind on this backend - a multi-row ``VALUES``
        binds ``column_count`` parameters per row.

        Args:
            requested: The caller's batch_size, or None for the largest safe one. 0 or a negative
                value is passed through - ``get_batches()`` rejects it.
            column_count: Bind parameters per row.
            max_bind_parameters: The backend's bind-parameter ceiling
                (``Features.max_bind_parameters``).

        Returns:
            A batch size keeping a statement within ``max_bind_parameters``.
        """
        max_rows = max(1, max_bind_parameters // max(1, column_count))
        return min(requested, max_rows) if requested is not None else max_rows

    @staticmethod
    def get_instance_writers(
        model: type[Model],
        columns: tuple[str, ...],
        types: TypeRegistry,
    ) -> tuple[tuple[str, Callable[..., Any]], ...]:
        """The writers of a column layout (``TypeRegistry.get_db_writer()``) - a datetime's and a time's depend
        on ``use_timezone``."""
        bucket = BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.get_bucket(model)
        key = (columns, types, ZoneSettings.get_use_timezone())
        writers = bucket.get(key)
        if writers is None:
            writers = tuple((name, types.get_db_writer(model._meta.fields_map[name])) for name in columns)
            bucket.store(key, writers)
        return writers

    @staticmethod
    def serialize_instance(model: type[TModel], types: TypeRegistry, obj: TModel, columns: Sequence[str]) -> list[Any]:
        """Convert one object's values through its Python field writers."""
        writers = BulkWriteBatches.get_instance_writers(model, tuple(columns), types)
        return [writer(getattr(obj, name), obj) for name, writer in writers]

    @staticmethod
    def get_sensitive_positions(model: type[Model], field_names: Sequence[str]) -> list[int]:
        """The positions of the ``sensitive=True`` fields among a row's written fields - their
        parameters are never shown (``QueryParameters``).

        Args:
            model: The written model.
            field_names: The fields of a row, in order.

        Returns:
            The positions, none for a model without sensitive fields.
        """
        sensitive_fields = model._meta.sensitive_fields
        if not sensitive_fields:
            return []
        return [position for position, name in enumerate(field_names) if name in sensitive_fields]

    @staticmethod
    def hide_row_values(rows: list[Any], sensitive_positions: list[int]) -> list[Any]:
        """Rows of ``execute_many()``, the sensitive fields' values of each never shown.

        Args:
            rows: The rows.
            sensitive_positions: The sensitive fields' positions in a row.

        Returns:
            The rows - each a ``QueryParameters`` when a field is sensitive.
        """
        if not sensitive_positions:
            return rows
        return [QueryParameters(row, sensitive_positions) for row in rows]

    @staticmethod
    def serialize_instances(
        model: type[TModel],
        types: TypeRegistry,
        objs: list[TModel],
        columns: list[str],
    ) -> list[list[Any]]:
        """Convert every row through the same field writers as a single-object write."""
        writers = BulkWriteBatches.get_instance_writers(model, tuple(columns), types)
        return NativeModules.rows.serialize_instances(objs, writers)
