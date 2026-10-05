from __future__ import annotations

import re
from typing import Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model


class ColumnTypeChanges(SchemaEditorPart):
    """What an altered field changes in its column: the column attributes that differ, whether the type
    changes, and whether the database rewrites the table for it."""

    __slots__ = ()

    @staticmethod
    def get_altered_columns(
        old_model: type[Model], new_model: type[Model], field_name: str
    ) -> list[tuple[Field[Any], Field[Any]]]:
        """The columns an ``AlterField`` of a field changes, before and after.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.

        Returns:
            The field itself for a plain column, each key column for a forward relation - a plain
            field turned into one included - nothing for a many-to-many relation - its through
            table holds its columns.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if isinstance(old_field, ManyToManyFieldInstance) or isinstance(new_field, ManyToManyFieldInstance):
            return []
        old_columns = ColumnTypeChanges.get_columns(old_model, old_field, field_name)
        new_columns = ColumnTypeChanges.get_columns(new_model, new_field, field_name)
        if len(old_columns) == len(new_columns):
            return list(zip(old_columns, new_columns, strict=True))
        # One column turned into a composite key, or back: each column compared with each.
        return [(old_column, new_column) for old_column in old_columns for new_column in new_columns]

    @staticmethod
    def get_columns(model: type[Model], field: Field[Any], field_name: str) -> list[Field[Any]]:
        """The fields of a field's columns.

        Args:
            model: The model.
            field: The field.
            field_name: The field's name.

        Returns:
            Each key field of a forward relation, the field itself otherwise.
        """
        if not isinstance(field, ForeignKeyFieldInstance):
            return [field]
        key_names = field.source_fields if len(field.source_fields) > 1 else (field.source_field or field_name,)
        return [model._meta.fields_map[key_name] for key_name in key_names]

    @classmethod
    def changes_column_type(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """Whether an ``AlterField`` of a field changes the type of one of its columns - its values
        are converted, and a value the new type can't hold is lost.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when a column's type in the database changes.
        """
        return any(
            old_column.get_column_type(dialect) != new_column.get_column_type(dialect)
            for old_column, new_column in cls.get_altered_columns(old_model, new_model, field_name)
        )

    @classmethod
    def rewrites_table_on_alter(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """Whether the database rewrites the whole table to alter a field - the time it takes grows
        with the table's rows. Changing a column's type does.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when the table is rewritten.
        """
        return cls.changes_column_type(old_model, new_model, field_name, dialect)

    def get_type_change_sql(self, old_field: Field[Any], new_field: Field[Any], column: str, sql_type: str) -> str:
        """The ``ALTER TABLE`` change giving a field's column its new type.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            column: The quoted column.
            sql_type: The column's new type.

        Returns:
            The change.
        """
        return self.editor.ALTER_FIELD_TYPE_TEMPLATE.format(column=column, sql_type=sql_type)

    def get_generated_fields_depending_on_column(self, model: type[Model], db_field: str) -> list[Field[Any]]:
        """Non-pk GeneratedField entries on `model` whose own expression references `db_field`.

        Args:
            model: The model to scan.
            db_field: The database column name to look for.

        Returns:
            The dependent GeneratedField instances.
        """
        dependent_fields: list[Field[Any]] = []
        for field in model._meta.fields_map.values():
            if not isinstance(field, GeneratedField) or field.pk:
                continue
            expressions = field.expression.values() if isinstance(field.expression, dict) else [field.expression]
            if any(re.search(rf"\b{re.escape(db_field)}\b", expression.sql) for expression in expressions):
                dependent_fields.append(field)
        return dependent_fields
