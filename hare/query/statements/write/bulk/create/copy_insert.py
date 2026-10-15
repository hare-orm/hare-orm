from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import UnSupportedError
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.default_rows_insert import DefaultRowsInsert

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class CopyInsert:
    """A bulk insert through the dialect's bulk load (``DatabaseClient.copy()``), in batches - the
    column types declared, every type checked before any batch is loaded."""

    @staticmethod
    def get_copy_column_types(bulk_create: BulkCreateQuery[Any], field_names: list[str]) -> list[str]:
        """Each field's column type as the dialect names it for a bulk load.

        Args:
            bulk_create: The bulk insert.
            field_names: The fields being copied, in column order.

        Returns:
            One type name per field.

        Raises:
            UnSupportedError: The bulk load doesn't load a field's type.
        """
        parameters = bulk_create._connection.dialect.parameters
        fields_map = bulk_create.model._meta.fields_map
        column_types = [
            parameters.get_copy_column_type(fields_map[copied_field_name]) for copied_field_name in field_names
        ]
        for field_name, column_type in zip(field_names, column_types, strict=True):
            if not parameters.supports_copy_column_type(column_type):
                raise UnSupportedError(
                    f"bulk_create(use_copy=True) does not support field '{field_name}' "
                    f"(column type {column_type!r}) - the bulk load of {bulk_create._connection.dialect.name} doesn't "
                    "load that type; use the default multi-row INSERT path (use_copy=False) for this "
                    "model instead."
                )
        return column_types

    @staticmethod
    def check_copy_supported_types(bulk_create: BulkCreateQuery[Any], omit_fields: set[str]) -> None:
        """Checks the column types of every group ``execute_via_copy()`` loads before any is loaded -
        the two groups copy separately.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left out of the INSERT because every object relies on its DB
                default.

        Raises:
            UnSupportedError: The bulk load doesn't load a field's type.
        """
        has_custom_pk_objects = any(obj._custom_generated_pk for obj in bulk_create._objects)
        has_regular_objects = any(not obj._custom_generated_pk for obj in bulk_create._objects)
        if has_custom_pk_objects:
            CopyInsert.get_copy_column_types(
                bulk_create, bulk_create._filtered_field_names(omit_fields, include_generated=True)
            )
        if has_regular_objects:
            CopyInsert.get_copy_column_types(bulk_create, bulk_create._filtered_field_names(omit_fields))

    @staticmethod
    async def copy_objects(
        bulk_create: BulkCreateQuery[Any], db_columns: list[str], field_names: list[str], objects: list[TModel]
    ) -> None:
        """Loads ``objects`` through the dialect's bulk load, in batches of ``batch_size`` - it has
        no bind-parameter ceiling.

        Args:
            bulk_create: The bulk insert.
            db_columns: The columns loaded.
            field_names: The fields read off each object, one per column.
            objects: The objects loaded.
        """
        if not objects or not db_columns:
            return
        table = bulk_create.model._meta.db_table
        column_types = CopyInsert.get_copy_column_types(bulk_create, field_names)
        for objects_item in BulkWriteBatches.get_batches(objects, bulk_create._batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            rows = BulkWriteBatches.serialize_instances(
                bulk_create.model, bulk_create._connection.dialect.types, objects_item, field_names
            )
            await bulk_create._connection.copy(table, db_columns, [tuple(row) for row in rows], column_types)

    @staticmethod
    async def execute_via_copy(
        bulk_create: BulkCreateQuery[Any],
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        """The ``use_copy=True`` counterpart of ``_execute_many()``: the same two groups, loaded
        through ``CopyInsert.copy_objects()``; a group with no column to write falls back to DEFAULT VALUES
        rows.

        Args:
            bulk_create: The bulk insert.
            insert_sql: The INSERT of the objects whose keys the database generates.
            insert_sql_all: The INSERT of the objects with keys of their own.
            effective_columns: The fields written for the former.
            effective_columns_all: The fields written for the latter.
            omit_fields: The fields left out of the insert.
        """
        custom_pk_instances = [instance for instance in bulk_create._objects if instance._custom_generated_pk]
        regular_instances = [instance for instance in bulk_create._objects if not instance._custom_generated_pk]

        if effective_columns_all:
            await CopyInsert.copy_objects(
                bulk_create,
                bulk_create._filter_columns(omit_fields, include_generated=True),
                effective_columns_all,
                custom_pk_instances,
            )
        elif custom_pk_instances:
            await DefaultRowsInsert.insert_default_rows(
                bulk_create, insert_sql_all, custom_pk_instances, omit_fields, False
            )

        if effective_columns:
            await CopyInsert.copy_objects(
                bulk_create,
                bulk_create._filter_columns(omit_fields),
                effective_columns,
                regular_instances,
            )
        elif regular_instances:
            await DefaultRowsInsert.insert_default_rows(bulk_create, insert_sql, regular_instances, omit_fields, False)
