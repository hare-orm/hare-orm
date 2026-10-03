from __future__ import annotations

from collections.abc import Callable, Iterable
from itertools import batched
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from hare.core.model_cache import ModelCache
from hare.exceptions import (
    QueryError,
)
from hare.query.plans.statement_plans import StatementPlans
from hare.query.rows.hydrate_accelerator import HydrateAccelerator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkWriteBatches:
    """How bulk writes split their objects into statements and serialize them."""

    SERIALIZE_INSTANCES_CACHE: ClassVar[
        ModelCache[dict[tuple[tuple[str, ...], TypeRegistry], Callable[[Any], list[Any]]]]
    ] = ModelCache()

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
            raise QueryError(f"batch_size must be a positive integer, got {batch_size!r}")
        yield from batched(instances, batch_size)

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
        sql = values_rows_sql.get_for_model(model, key)
        if sql is None:
            sql = values_rows_sql[(model, *key)] = BulkWriteBatches.build_values_rows_sql(*key)
        return cast("str", sql)

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
    def get_bind_param_safe_batch_size(requested: int | None, num_columns: int, max_bind_params: int) -> int:
        """The number of rows one multi-row statement can bind on this backend - a multi-row ``VALUES``
        binds ``num_columns`` parameters per row.

        Args:
            requested: The caller's batch_size, or None for the largest safe one. 0 or a negative
                value is passed through - ``get_batches()`` rejects it.
            num_columns: Bind parameters per row.
            max_bind_params: The backend's bind-parameter ceiling
                (``Features.max_bind_parameters``).

        Returns:
            A batch size keeping a statement within ``max_bind_params``.
        """
        max_rows = max(1, max_bind_params // max(1, num_columns))
        return min(requested, max_rows) if requested is not None else max_rows

    @staticmethod
    def get_compiled_instance_serializer(
        model: type[Model], columns: tuple[str, ...], types: TypeRegistry
    ) -> Callable[[Any], list[Any]]:
        """Compiles, once per model, columns and type registry, a function building an instance's row:
        one literal attribute read per column with the column's writer bound in - no field lookup or
        ``getattr()`` per value.
        """
        per_model = BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.get(model)
        if per_model is None:
            per_model = BulkWriteBatches.SERIALIZE_INSTANCES_CACHE[model] = {}
        cached = per_model.get((columns, types))
        if cached is not None:
            return cached
        fields_map = model._meta.fields_map
        namespace: dict[str, Any] = {}
        lines = ["def _serialize(instance):", "    return ["]
        for i, field_name in enumerate(columns):
            writer_ref = f"_writer_{i}"
            namespace[writer_ref] = types.get_db_writer(fields_map[field_name])
            lines.append(f"        {writer_ref}(instance.{field_name}, instance),")
        lines.append("    ]")
        exec("\n".join(lines), namespace)  # noqa: S102 # nosec B102 - columns-derived source, no external input
        compiled = cast("Callable[[Any], list[Any]]", namespace["_serialize"])
        per_model[(columns, types)] = compiled
        return compiled

    @staticmethod
    def serialize_instance(
        model: type[TModel], types: TypeRegistry, instance: TModel, columns: list[str]
    ) -> list[Any]:
        """Builds the DB-ready row of one instance, in ``columns`` order - by the Rust accelerator when it
        is built, else by the compiled Python function.
        """
        if HydrateAccelerator.module is not None:
            try:
                writer = HydrateAccelerator.get_model_writer(model, tuple(columns), types)
                return cast("list[Any]", writer.write_row(instance))
            except (TypeError, AttributeError) as exc:
                # As in serialize_instances(): an object the caller got wrong fails the Python path too.
                row = BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)(instance)
                HydrateAccelerator.disable(exc)
                return row
        return BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)(instance)

    @staticmethod
    def serialize_instances(
        model: type[TModel], types: TypeRegistry, instances: list[TModel], columns: list[str]
    ) -> list[list[Any]]:
        """Builds one DB-ready row of values per instance, in ``columns`` order - by the Rust
        accelerator when it is built, else by the compiled Python function. Empty ``instances`` or
        ``columns`` give ``[]``.
        """
        if not instances or not columns:
            return []
        serialize = BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)
        if HydrateAccelerator.module is not None:
            try:
                writer = HydrateAccelerator.get_model_writer(model, tuple(columns), types)
                return cast("list[list[Any]]", writer.write_rows(instances))
            except (TypeError, AttributeError) as exc:
                # The pure-Python path decides whose fault it was: an object the caller got wrong
                # fails there the same way and propagates, leaving the accelerator on; only when it
                # succeeds was the compiled rust.native.rows out of step with this call (a stale build).
                rows = [serialize(instance) for instance in instances]
                HydrateAccelerator.disable(exc)
                return rows
        return [serialize(instance) for instance in instances]
