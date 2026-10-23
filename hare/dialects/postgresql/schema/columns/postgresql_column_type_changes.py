from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.columns.column_type_changes import ColumnTypeChanges
from hare.dialects.postgresql.fields.multiranges.multi_range_field import MultiRangeField
from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.dialects.postgresql.schema.constants import (
    POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES,
    POSTGRES_COLUMN_TYPE_RE,
    POSTGRES_NUMERIC_TYPE_NAMES,
    POSTGRES_VARCHAR_TYPE_NAMES,
    POSTGRESQL_ALTER_FIELD_TYPE_THROUGH_TEXT_TEMPLATE,
)
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlColumnTypeChanges(ColumnTypeChanges):
    """ColumnTypeChanges as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_type_change_sql(self, old_field: Field[Any], new_field: Field[Any], column: str, sql_type: str) -> str:
        """A range (or multirange) column becomes a range of another type, and a column of an ``ENUM``
        type (an array of one too) one of another type, through its text, which PostgreSQL has no cast
        for; any other column is cast to its new type.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            column: The quoted column.
            sql_type: The column's new type.

        Returns:
            The change.
        """
        old_shape = self.get_range_shape(old_field)
        old_enum_type, new_enum_type = old_field.requires_enum_type, new_field.requires_enum_type
        if (old_shape is not None and old_shape == self.get_range_shape(new_field)) or (
            old_enum_type is not None and new_enum_type is not None and old_enum_type.key != new_enum_type.key
        ):
            return POSTGRESQL_ALTER_FIELD_TYPE_THROUGH_TEXT_TEMPLATE.format(column=column, sql_type=sql_type)
        return super().get_type_change_sql(old_field, new_field, column, sql_type)

    @staticmethod
    def get_range_shape(field: Field[Any]) -> tuple[type[Field[Any]], int] | None:
        """What a field holding ranges holds: a range or a multirange, inside how many arrays.

        Args:
            field: The field.

        Returns:
            The range field class and the array depth; None for a field holding no ranges.
        """
        depth = 0
        while isinstance(field, ArrayField):
            field = field.base_field
            depth += 1
        for range_class in (MultiRangeField, RangeField):
            if isinstance(field, range_class):
                return range_class, depth
        return None

    @classmethod
    def rewrites_table_on_alter(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """PostgreSQL rewrites the table for a column type change, except to a type that holds every
        old value as it is - a longer or unlimited ``varchar``, ``text``, a ``numeric`` of the same
        scale and more digits.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when the table is rewritten.
        """
        return any(
            cls.column_type_change_rewrites_table(
                old_column.get_column_type(dialect), new_column.get_column_type(dialect)
            )
            for old_column, new_column in cls.get_altered_columns(old_model, new_model, field_name)
        )

    @staticmethod
    def column_type_change_rewrites_table(old_type: str, new_type: str) -> bool:
        """Whether changing a column from one type to another rewrites the table.

        Args:
            old_type: The column's type before, as hare writes it.
            new_type: Its type after.

        Returns:
            False for the same type, or one holding every old value as it is.
        """
        if old_type.strip().lower() == new_type.strip().lower():
            return False
        old_match = POSTGRES_COLUMN_TYPE_RE.match(old_type)
        new_match = POSTGRES_COLUMN_TYPE_RE.match(new_type)
        if old_match is None or new_match is None:
            return True
        old_name, new_name = old_match["name"].lower(), new_match["name"].lower()
        old_arguments, new_arguments = old_match["arguments"], new_match["arguments"]
        if old_name in POSTGRES_VARCHAR_TYPE_NAMES and new_name in POSTGRES_VARCHAR_TYPE_NAMES:
            if new_arguments is None:
                return False
            return old_arguments is None or int(new_arguments) < int(old_arguments)
        if (old_name, new_name) in POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES:
            return new_arguments is not None
        if old_name in POSTGRES_NUMERIC_TYPE_NAMES and new_name in POSTGRES_NUMERIC_TYPE_NAMES:
            if new_arguments is None:
                return False
            if old_arguments is None:
                return True
            old_precision, _, old_scale = old_arguments.partition(",")
            new_precision, _, new_scale = new_arguments.partition(",")
            return int(new_scale or 0) != int(old_scale or 0) or int(new_precision) < int(old_precision)
        return True
