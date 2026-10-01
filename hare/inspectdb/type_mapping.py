"""Maps an introspected database column onto the hare-orm field class that reconstructs it - the
single mapping both the model source generator and the runtime model factory use."""

from __future__ import annotations

from typing import Any

from hare.dialects.registry import DialectRegistry
from hare.fields.enums import OnDelete
from hare.inspectdb.exceptions import UnsupportedDialectError
from hare.inspectdb.types.column_info import ColumnInfo
from hare.migrations.writer.migration_writer import MigrationWriter


class ColumnTypeMapper:
    """Column-to-field mapping rules - all static methods, no instance state."""

    @staticmethod
    def map_column_type(dialect: str, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        """Maps a column onto a field class path and its type kwargs, by the dialect's introspector. An
        array's element field comes under ``BASE_FIELD_SENTINEL_KWARG``, an ambiguity reason under
        ``AMBIGUOUS_REASON_SENTINEL_KWARG`` - both popped by the caller.

        Args:
            dialect: The dialect's name.
            column: The column.

        Returns:
            (field_path, extra_kwargs, is_ambiguous) - an ambiguous type becomes a TextField.

        Raises:
            UnsupportedDialectError: The dialect has no introspector.
        """
        introspector_class = DialectRegistry.get_dialect(dialect).introspector_class
        if introspector_class is None:
            raise UnsupportedDialectError(dialect)
        return introspector_class.map_column_type(column)

    @staticmethod
    def is_indexable_field_path(path: str) -> bool:
        """Whether the field class at ``path`` accepts unique=/db_index= and index membership.

        Args:
            path: Dotted field class path, as returned by map_column_type().

        Returns:
            The field class's own ``indexable`` flag.
        """
        return bool(getattr(MigrationWriter.get_callable(path), "indexable", True))

    @staticmethod
    def get_set_default_fallback(
        on_delete: OnDelete, has_column_default: bool, nullable: bool
    ) -> tuple[OnDelete, str | None]:
        """Maps ON DELETE SET DEFAULT onto what a ForeignKeyField takes: without a column DEFAULT the
        database sets NULL (nullable) or fails the delete (NOT NULL) - SET_NULL / NO_ACTION.

        Args:
            on_delete: The action read off the database.
            has_column_default: Whether every key column has a DEFAULT.
            nullable: Whether the key columns accept NULL.

        Returns:
            The on_delete, and a TODO reason when it differs from the database's.
        """
        if on_delete != OnDelete.SET_DEFAULT or has_column_default:
            return on_delete, None
        fallback = OnDelete.SET_NULL if nullable else OnDelete.NO_ACTION
        return fallback, (
            f"the database declares ON DELETE SET DEFAULT but the column has no DEFAULT, so it resets to "
            f"NULL or fails - rendered as {fallback.name}; add a db_default and on_delete=SET_DEFAULT "
            "if a real fallback row is intended"
        )
