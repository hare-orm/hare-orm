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
            The field itself for a plain column, each key column for a forward relation, nothing
            for a many-to-many relation - its through table holds its columns.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if isinstance(old_field, ManyToManyFieldInstance) or isinstance(new_field, ManyToManyFieldInstance):
            return []
        if isinstance(old_field, ForeignKeyFieldInstance) and isinstance(new_field, ForeignKeyFieldInstance):
            old_key_names = (
                old_field.source_fields
                if len(old_field.source_fields) > 1
                else (old_field.source_field or field_name,)
            )
            new_key_names = (
                new_field.source_fields
                if len(new_field.source_fields) > 1
                else (new_field.source_field or field_name,)
            )
            return [
                (old_model._meta.fields_map[old_key_name], new_model._meta.fields_map[new_key_name])
                for old_key_name, new_key_name in zip(old_key_names, new_key_names, strict=True)
            ]
        return [(old_field, new_field)]

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
