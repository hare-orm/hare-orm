from __future__ import annotations

from typing import Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.model_binding import ModelBinding


class ColumnTypeChanges(SchemaEditorPart):
    """What an altered field changes in its column: the column attributes that differ, whether the type
    changes, and whether the database rewrites the table for it."""

    __slots__ = ()

    @staticmethod
    def get_altered_columns(
        old_binding: ModelBinding, new_binding: ModelBinding, field_name: str
    ) -> list[tuple[Field[Any], Field[Any]]]:
        """The columns an ``AlterField`` of a field changes, before and after.

        Args:
            old_binding: The binding of the model rendered from the state before the change.
            new_binding: The binding of the model rendered from the state after it.
            field_name: The altered field's name.

        Returns:
            The field itself for a plain column, each key column for a forward relation - a plain
            field turned into one included - nothing for a many-to-many relation - its through
            table holds its columns.
        """
        old_field = old_binding.fields_map[field_name]
        new_field = new_binding.fields_map[field_name]
        if isinstance(old_field, ManyToManyFieldInstance) or isinstance(new_field, ManyToManyFieldInstance):
            return []
        old_columns = ColumnTypeChanges.get_columns(old_binding, old_field, field_name)
        new_columns = ColumnTypeChanges.get_columns(new_binding, new_field, field_name)
        if len(old_columns) == len(new_columns):
            return list(zip(old_columns, new_columns, strict=True))
        # One column turned into a composite key, or back: each column compared with each.
        return [(old_column, new_column) for old_column in old_columns for new_column in new_columns]

    @staticmethod
    def get_columns(binding: ModelBinding, field: Field[Any], field_name: str) -> list[Field[Any]]:
        """The fields of a field's columns.

        Args:
            binding: The model.
            field: The field.
            field_name: The field's name.

        Returns:
            Each key field of a forward relation, the field itself otherwise.
        """
        if not isinstance(field, ForeignKeyFieldInstance):
            return [field]
        source_fields = field.source_fields
        key_names = source_fields if len(source_fields) > 1 else (field.source_field or field_name,)
        return [binding.fields_map[key_name] for key_name in key_names]

    @classmethod
    def changes_column_type(
        cls, old_binding: ModelBinding, new_binding: ModelBinding, field_name: str, dialect: Dialect
    ) -> bool:
        """Whether an ``AlterField`` of a field changes the type of one of its columns - its values
        are converted, and a value the new type can't hold is lost.

        Args:
            old_binding: The binding of the model rendered from the state before the change.
            new_binding: The binding of the model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when a column's type in the database changes.
        """
        return any(
            old_column.get_column_type(dialect) != new_column.get_column_type(dialect)
            for old_column, new_column in cls.get_altered_columns(old_binding, new_binding, field_name)
        )

    @staticmethod
    def column_type_change_rewrites_table(old_type: str, new_type: str) -> bool:
        """Whether the prepared type-change statement rewrites stored rows."""
        return old_type != new_type

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

    def get_generated_fields_depending_on_column(self, binding: ModelBinding, db_field: str) -> list[Field[Any]]:
        """The non-pk fields of `model` whose generated column reads `db_field` - a GeneratedField's expression
        of hare (a raw SQL expression's columns aren't read out of it) or a field naming the fields it is
        computed from (a TSVectorField's source_fields).

        Args:
            binding: The model to scan.
            db_field: The database column name to look for.

        Returns:
            The dependent fields.
        """
        read_field_names = {name for name, column in binding.fields_db_projection.items() if column == db_field}
        # A relation's key column is read through the relation's own name too.
        read_field_names |= {
            name
            for name, field in binding.fields_map.items()
            if read_field_names.intersection(getattr(field, "source_fields", None) or ())
        }
        return [
            field
            for field in binding.fields_map.values()
            if not field.pk and read_field_names.intersection(field.get_generated_from_field_names())
        ]
