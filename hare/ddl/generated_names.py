from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING

from hare.ddl.enums import GeneratedNamePrefix
from hare.dialects.base.constants import (
    FOREIGN_KEY_NAME_HASH_LENGTH,
    FOREIGN_KEY_NAME_TABLE_PREFIX_LENGTH,
    INDEX_EXPRESSION_MARKER_CHARS,
    INDEX_NAME_FIELD_PREFIX_LENGTH,
    INDEX_NAME_HASH_LENGTH,
    INDEX_NAME_TABLE_PREFIX_LENGTH,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class GeneratedNames:
    """The names hare gives indexes, constraints and foreign keys declared without one - pure
    functions of the table and column names, so ``generate_schemas()``, migrations, drift and
    inspectdb all arrive at the same name."""

    @staticmethod
    def is_index_expression(key: str) -> bool:
        """Whether an index key is a rendered expression rather than a column name.

        Args:
            key: The key.
        """
        return any(token in key for token in INDEX_EXPRESSION_MARKER_CHARS)

    @staticmethod
    def get_hash(*parts: str, length: int) -> str:
        """A hex digest of the parts, cut to ``length``.

        Args:
            parts: The parts.
            length: The digest's length.

        Returns:
            The digest.
        """
        return sha256(";".join(parts).encode("utf-8")).hexdigest()[:length]

    @classmethod
    def get_index_name(
        cls,
        prefix: GeneratedNamePrefix,
        model: type[Model] | str,
        field_names: Sequence[str],
        name_parts: Sequence[str] = (),
    ) -> str:
        """The name of an index or a unique constraint.

        Args:
            prefix: The name's prefix, e.g. ``idx``.
            model: The model or its table name.
            field_names: The indexed columns or rendered expressions.
            name_parts: What else sets the index apart from another one on the same columns
                (access method, operator classes, storage parameters, condition).

        Returns:
            The name.
        """
        table_name = model if isinstance(model, str) else model._meta.db_table
        first_field = field_names[0]
        # An expression key holds SQL - it is left out of the readable part, the hash tells
        # expressions apart.
        field = "expr" if cls.is_index_expression(first_field) else first_field[:INDEX_NAME_FIELD_PREFIX_LENGTH]
        hashed = cls.get_hash(table_name, *field_names, *name_parts, length=INDEX_NAME_HASH_LENGTH)
        return f"{prefix}_{table_name[:INDEX_NAME_TABLE_PREFIX_LENGTH]}_{field}_{hashed}"

    @classmethod
    def get_foreign_key_name(
        cls, from_table: str, from_fields: Sequence[str], to_table: str, to_fields: Sequence[str]
    ) -> str:
        """The name of a foreign key constraint.

        Args:
            from_table: The referencing table.
            from_fields: Its key columns.
            to_table: The referenced table.
            to_fields: The referenced columns.

        Returns:
            The name.
        """
        hashed = cls.get_hash(from_table, *from_fields, to_table, *to_fields, length=FOREIGN_KEY_NAME_HASH_LENGTH)
        from_prefix = from_table[:FOREIGN_KEY_NAME_TABLE_PREFIX_LENGTH]
        to_prefix = to_table[:FOREIGN_KEY_NAME_TABLE_PREFIX_LENGTH]
        return f"fk_{from_prefix}_{to_prefix}_{hashed}"
