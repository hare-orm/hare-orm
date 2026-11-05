from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_type_changes import ColumnTypeChanges
from hare.dialects.postgresql.fields.multiranges.multi_range_field import MultiRangeField
from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.dialects.postgresql.schema.constants import (
    POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES,
    POSTGRES_NUMERIC_TYPE_NAMES,
    POSTGRES_VARCHAR_TYPE_NAMES,
    POSTGRESQL_ALTER_FIELD_TYPE_THROUGH_TEXT_TEMPLATE,
)
from hare.dialects.postgresql.types.postgresql_physical_type import PostgresqlPhysicalType
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field

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

    @staticmethod
    def column_type_change_rewrites_table(old_type: str, new_type: str) -> bool:
        """Whether changing a column from one type to another rewrites the table.

        Args:
            old_type: The column's type before, as hare writes it.
            new_type: Its type after.

        Returns:
            False for the same type, or one holding every old value as it is.
        """
        old = PostgresqlPhysicalType.from_sql(old_type)
        new = PostgresqlPhysicalType.from_sql(new_type)
        if old.sql == new.sql or (old.canonical_sql is not None and old.canonical_sql == new.canonical_sql):
            return False
        # Opaque catalog identities and arrays need a conservative rewrite decision.
        if old.name is None or new.name is None or old.array_dimensions or new.array_dimensions:
            return True
        if old.name in POSTGRES_VARCHAR_TYPE_NAMES and new.name in POSTGRES_VARCHAR_TYPE_NAMES:
            if new.parameters is None:
                return False
            return old.parameters is None or int(new.parameters[0]) < int(old.parameters[0])
        if (old.name, new.name) in POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES:
            return new.parameters is not None
        if old.name in POSTGRES_NUMERIC_TYPE_NAMES and new.name in POSTGRES_NUMERIC_TYPE_NAMES:
            if new.parameters is None:
                return False
            old_numeric, new_numeric = old.numeric_precision_scale, new.numeric_precision_scale
            if old_numeric is None or new_numeric is None:
                return True
            return new_numeric[1] != old_numeric[1] or new_numeric[0] < old_numeric[0]
        return True
