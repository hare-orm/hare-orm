from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.columns.column_type_changes import ColumnTypeChanges
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteColumnTypeChanges(ColumnTypeChanges):
    """ColumnTypeChanges as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    @classmethod
    def rewrites_table_on_alter(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """SQLite alters a column only by rebuilding its table - everything but a pure rename of a
        plain column, and a many-to-many relation, whose through table holds its columns.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when the table is rebuilt.
        """
        altered_columns = cls.get_altered_columns(old_model, new_model, field_name)
        if not altered_columns:
            return False
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        return not cls.only_renames_column(old_field, new_field, dialect) and not cls.keeps_table(
            old_field, new_field, dialect
        )

    @classmethod
    def keeps_table(cls, old_field: Field[Any], new_field: Field[Any], dialect: Dialect) -> bool:
        """Whether a field's column is declared in ``CREATE TABLE`` as it was and its values stay
        as stored - the field's Python side changed (``sensitive``, ``encrypt_keys``, ...), or its
        own index, which is a statement of its own; neither needs a rebuild.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            dialect: The database's dialect.

        Returns:
            True when the table's declaration of the column is the same.
        """
        return (
            not isinstance(old_field, (ForeignKeyFieldInstance, ManyToManyFieldInstance))
            and type(old_field) is type(new_field)
            and new_field.get_narrowing_limit(old_field) is None
            and TableRebuild.get_remake_column_name(old_field) == TableRebuild.get_remake_column_name(new_field)
            and cls.get_column_declaration(old_field, dialect) == cls.get_column_declaration(new_field, dialect)
        )

    @staticmethod
    def get_column_declaration(field: Field[Any], dialect: Dialect) -> tuple[Any, ...]:
        """What a field's column is declared with in ``CREATE TABLE``.

        Args:
            field: The field.
            dialect: The database's dialect.

        Returns:
            The declaration's parts.
        """
        return (
            field.get_column_type(dialect),
            field.null,
            field.unique,
            field.pk,
            field.generated,
            getattr(field, "stored", True),
            field.get_generated_sql(dialect) if field.generated else None,
            field.db_default if field.has_db_default() else None,
            field.description,
        )

    @classmethod
    def only_renames_column(cls, old_field: Field[Any], new_field: Field[Any], dialect: Dialect) -> bool:
        """Whether altering a field only renames its column - ``RENAME COLUMN`` does it; any other
        change rebuilds the table.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            dialect: The database's dialect.

        Returns:
            True when the column's name is all that changes.
        """
        return (
            not isinstance(old_field, ForeignKeyFieldInstance)
            and TableRebuild.get_remake_column_name(old_field) != TableRebuild.get_remake_column_name(new_field)
            and old_field.null == new_field.null
            and old_field.unique == new_field.unique
            and old_field.index == new_field.index
            and getattr(old_field, "db_default", DB_DEFAULT_NOT_SET)
            == getattr(new_field, "db_default", DB_DEFAULT_NOT_SET)
            and old_field.get_column_type(dialect) == new_field.get_column_type(dialect)
            and not old_field.pk
            and not new_field.pk
        )
