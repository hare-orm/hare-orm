from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.columns.column_type_changes import ColumnTypeChanges
from hare.dialects.postgresql.schema.constants import (
    POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES,
    POSTGRES_COLUMN_TYPE_RE,
    POSTGRES_NUMERIC_TYPE_NAMES,
    POSTGRES_VARCHAR_TYPE_NAMES,
)
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlColumnTypeChanges(ColumnTypeChanges):
    """ColumnTypeChanges as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

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
