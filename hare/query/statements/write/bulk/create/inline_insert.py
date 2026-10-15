from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.template_insert import TemplateInsert

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class InlineInsert:
    """INSERT statements with every value rendered into the SQL - one multi-row statement per group of
    objects - for showing and explaining a bulk insert."""

    @staticmethod
    def build_inline_insert_sql(
        bulk_create: BulkCreateQuery[Any],
        field_names: list[str],
        db_columns: list[str],
        objects: list[TModel],
        omit_fields: set[str],
    ) -> str:
        """The INSERT of ``objects`` with every value rendered into the SQL - one multi-row statement
        per batch, for ``sql(parameters_inline=True)``.

        Args:
            bulk_create: The bulk insert.
            field_names: The fields read off each object, one per column.
            db_columns: The columns written.
            objects: The objects inserted.
            omit_fields: The fields left out of the insert.
        """
        if not objects:
            return ""
        if not db_columns:
            return ";".join([TemplateInsert.build_default_values_sql(bulk_create, omit_fields)] * len(objects))

        fields_map = bulk_create.model._meta.fields_map
        insert_statement = bulk_create._get_insert_statement()
        conflict = ConflictClause.get_conflict(bulk_create)
        statements = []
        for objects_item in BulkWriteBatches.get_batches(objects, bulk_create._batch_size):
            if not objects_item:
                continue
            rows = [
                [
                    bulk_create.dialect.types.get_db_value(fields_map[field_name], getattr(obj, field_name), obj)
                    for field_name in field_names
                ]
                for obj in objects_item
            ]
            statements.append(str(insert_statement.get_query(db_columns, rows, conflict=conflict)))
        return ";".join(statements)

    @staticmethod
    def make_inline_statements(bulk_create: BulkCreateQuery[Any], omit_fields: set[str]) -> list[str]:
        """The INSERT statements with every value rendered in - one per group of objects (without
        and with a caller-given primary key).

        Args:
            bulk_create: The bulk insert.
            omit_fields: The fields left out of the insert.
        """
        custom_pk_objects = [instance for instance in bulk_create._objects if instance._custom_generated_pk]
        regular_objects = [instance for instance in bulk_create._objects if not instance._custom_generated_pk]

        statements = []
        if regular_objects:
            statements.append(
                InlineInsert.build_inline_insert_sql(
                    bulk_create,
                    bulk_create._filtered_field_names(omit_fields),
                    bulk_create._filter_columns(omit_fields),
                    regular_objects,
                    omit_fields,
                )
            )
        if custom_pk_objects:
            statements.append(
                InlineInsert.build_inline_insert_sql(
                    bulk_create,
                    bulk_create._filtered_field_names(omit_fields, include_generated=True),
                    bulk_create._filter_columns(omit_fields, include_generated=True),
                    custom_pk_objects,
                    omit_fields,
                )
            )
        return [statement for statement in statements if statement]
