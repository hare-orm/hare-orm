from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from itertools import batched
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import (
    QueryError,
)
from hare.instrumentation.observers.observers import Observers
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.sql.terms.parameters.query_parameters import QueryParameters

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
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
        for position, field_name in enumerate(columns):
            writer_reference = f"_writer_{position}"
            namespace[writer_reference] = types.get_db_writer(fields_map[field_name])
            lines.append(f"        {writer_reference}(instance.{field_name}, instance),")
        lines.append("    ]")
        exec("\n".join(lines), namespace)  # nosec B102 - columns-derived source, no external input
        compiled = cast("Callable[[Any], list[Any]]", namespace["_serialize"])
        per_model[(columns, types)] = compiled
        return compiled

    @staticmethod
    def serialize_instance(model: type[TModel], types: TypeRegistry, obj: TModel, columns: list[str]) -> list[Any]:
        """Builds the DB-ready row of one obj, in ``columns`` order - by the Rust accelerator when it
        is built, else by the compiled Python function.
        """
        if HydrateAccelerator.module is not None:
            try:
                writer = HydrateAccelerator.get_model_writer(model, tuple(columns), types)
                return writer.write_row(obj)
            except (TypeError, AttributeError) as error:
                # As in serialize_instances(): an object the caller got wrong fails the Python path too.
                row = BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)(obj)
                HydrateAccelerator.disable(error)
                return row
        return BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)(obj)

    @staticmethod
    def get_model_writer_for_parameters(model: type[TModel], connection: DatabaseClient, columns: list[str]) -> Any:
        """The ``rust.native.rows.ModelWriter`` that writes the parameters of ``connection``'s driver
        itself - where the driver binds them (``Features.binds_written_parameters``) and nothing is
        given the parameters to look at (a query observer, the DEBUG log); None otherwise.

        Args:
            model: The model.
            connection: The connection the statement runs on.
            columns: The written columns.

        Returns:
            The writer, or None.
        """
        if (
            not columns
            or HydrateAccelerator.module is None
            or not connection.features.binds_written_parameters
            or Observers.sees_query_parameters()
        ):
            return None
        return HydrateAccelerator.get_model_writer(model, tuple(columns), connection.dialect.types)

    @staticmethod
    def write_statement_parameters(
        model: type[TModel], connection: DatabaseClient, objs: list[TModel], columns: list[str], before: list[Any]
    ) -> Any | None:
        """The parameters of one statement binding ``before``, then the row of each instance, as the
        driver binds them - None where the rows are serialized as Python values.

        Args:
            model: The model.
            connection: The connection the statement runs on.
            objs: The objs, at least one.
            columns: The written columns, in order.
            before: The values bound before the rows.

        Returns:
            The parameters, or None.
        """
        try:
            writer = BulkWriteBatches.get_model_writer_for_parameters(model, connection, columns)
            return None if writer is None else writer.write_parameters(objs, before, ())
        except (TypeError, AttributeError) as error:
            # As in serialize_instances(): an object the caller got wrong fails the Python path too.
            BulkWriteBatches.serialize_instances(model, connection.dialect.types, objs, columns)
            HydrateAccelerator.disable(error)
            return None

    @staticmethod
    def write_statement_column_parameters(
        model: type[TModel], connection: DatabaseClient, objs: list[TModel], columns: list[str]
    ) -> Any | None:
        """The parameters of one statement binding an array per column - each column's values over
        the objs - as the driver binds them; None where the rows are serialized as Python
        values.

        Args:
            model: The model.
            connection: The connection the statement runs on.
            objs: The objs, at least one.
            columns: The written columns, in order.

        Returns:
            The parameters, or None.
        """
        try:
            writer = BulkWriteBatches.get_model_writer_for_parameters(model, connection, columns)
            return None if writer is None else writer.write_column_parameters(objs)
        except (TypeError, AttributeError) as error:
            BulkWriteBatches.serialize_instances(model, connection.dialect.types, objs, columns)
            HydrateAccelerator.disable(error)
            return None

    @staticmethod
    def write_statement_parameter_rows(
        model: type[TModel], connection: DatabaseClient, objs: list[TModel], columns: list[str], after: list[Any]
    ) -> Any | None:
        """The parameter rows of a statement run once per instance - its row, then ``after`` - as the
        driver binds them; None where the rows are serialized as Python values.

        Args:
            model: The model.
            connection: The connection the statement runs on.
            objs: The objs, at least one.
            columns: The written columns, in order.
            after: The values bound after each row.

        Returns:
            The parameter rows, or None.
        """
        try:
            writer = BulkWriteBatches.get_model_writer_for_parameters(model, connection, columns)
            return None if writer is None else writer.write_parameter_rows(objs, after)
        except (TypeError, AttributeError) as error:
            BulkWriteBatches.serialize_instances(model, connection.dialect.types, objs, columns)
            HydrateAccelerator.disable(error)
            return None

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
        model: type[TModel], types: TypeRegistry, objs: list[TModel], columns: list[str]
    ) -> list[list[Any]]:
        """Builds one DB-ready row of values per instance, in ``columns`` order - by the Rust
        accelerator when it is built, else by the compiled Python function. Empty ``objs`` or
        ``columns`` give ``[]``.
        """
        if not objs or not columns:
            return []
        serialize = BulkWriteBatches.get_compiled_instance_serializer(model, tuple(columns), types)
        if HydrateAccelerator.module is not None:
            try:
                writer = HydrateAccelerator.get_model_writer(model, tuple(columns), types)
                return cast("list[list[Any]]", writer.write_rows(objs))
            except (TypeError, AttributeError) as error:
                # The pure-Python path decides whose fault it was: an object the caller got wrong
                # fails there the same way and propagates, leaving the accelerator on; only when it
                # succeeds was the compiled rust.native.rows out of step with this call (a stale build).
                rows = [serialize(instance) for instance in objs]
                HydrateAccelerator.disable(error)
                return rows
        return [serialize(instance) for instance in objs]
