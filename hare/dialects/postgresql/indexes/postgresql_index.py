from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.indexes.partial_index import PartialIndex
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.fields.field import Field


class PostgresqlIndex(PartialIndex):
    """An index of a PostgreSQL access method - created and dropped on a PostgreSQL database only."""

    #: The extension giving the index's access method - None for a built-in one.
    REQUIRED_EXTENSION: ClassVar[str | None] = None
    #: The extension giving the access method operator classes for plain scalar columns - None when
    #: the access method needs none for them.
    SCALAR_KEY_EXTENSION: ClassVar[str | None] = None

    def get_required_extension(self, dialect: Dialect, fields_by_name: Mapping[str, Field[Any]]) -> str | None:
        """The extension of the access method, else the one giving it operator classes for a key over
        a plain scalar column without an operator class of its own - none on another database, which
        refuses the index.

        Args:
            dialect: The dialect.
            fields_by_name: The fields of the index's model, by name.
        """
        # Local import: the dialect's module imports its introspection, which imports the indexes.
        from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
        from hare.dialects.postgresql.types.postgresql_types import PostgresqlTypes

        if not isinstance(dialect, PostgresqlDialect):
            return None
        if self.REQUIRED_EXTENSION is not None:
            return self.REQUIRED_EXTENSION
        if self.SCALAR_KEY_EXTENSION is not None and not self.opclasses:
            if any(PostgresqlTypes.is_plain_scalar_field(fields_by_name.get(name)) for name in self.fields or ()):
                return self.SCALAR_KEY_EXTENSION
        return None

    def get_expression_dialect(self) -> Dialect:
        # The index exists on PostgreSQL only - its expressions (a SearchVector) are PostgreSQL's.
        # Local import: the dialect's modules import the indexes.
        from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

        return POSTGRESQL_DIALECT

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects the index on another database, and a key order PostgreSQL's indexes can't hold.

        Raises:
            UnSupportedError: The dialect isn't PostgreSQL's.
        """
        self.raise_if_other_dialect(dialect)
        super().raise_if_unsupported(dialect)

    def raise_if_not_droppable(self, features: Features, dialect: Dialect) -> None:
        """Rejects dropping the index on another database - it can't exist there.

        Raises:
            UnSupportedError: The dialect isn't PostgreSQL's.
        """
        self.raise_if_other_dialect(dialect)

    def raise_if_other_dialect(self, dialect: Dialect) -> None:
        """Rejects the index on a database of another dialect, before any SQL - the model or the
        migration names an index that database has no form of.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: The dialect isn't PostgreSQL's.
        """
        # Local import: the dialect's module imports its introspection, which imports the indexes.
        from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect

        if not isinstance(dialect, PostgresqlDialect):
            raise UnSupportedError(
                f"{type(self).__name__}(fields={list(self.get_declared_fields())!r}) is an index of PostgreSQL - the "
                f"{dialect} dialect has none: declare an index of that database in the model, or change the migration"
            )

    def validate_storage_parameter(self, name: str, value: Any, value_range: tuple[int, int]) -> int:
        """Checks an integer ``WITH (...)`` storage parameter before it's written into the DDL.

        Args:
            name: The parameter's name.
            value: The given value.
            value_range: The inclusive minimum and maximum.

        Returns:
            The value.

        Raises:
            ConfigurationError: The value isn't an int (a bool isn't one) or is out of range.
        """
        minimum, maximum = value_range
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{type(self).__name__} {name} must be an int, got {value!r}")
        if not minimum <= value <= maximum:
            raise ConfigurationError(
                f"{type(self).__name__} {name} must be between {minimum} and {maximum}, got "
                f"{ValueDescriptions.get_shown_text(value)}"
            )
        return value
