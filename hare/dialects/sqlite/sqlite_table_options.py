from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from hare.ddl.table_options import TableOptions
from hare.dialects.enums import DialectName
from hare.dialects.sqlite.server_versions import SQLITE_STRICT_SERVER_VERSION
from hare.dialects.sqlite.table_option_constants import (
    SQLITE_STRICT_COLUMN_TYPES,
    SQLITE_STRICT_TABLE_OPTION,
    SQLITE_WITHOUT_ROWID_TABLE_OPTION,
)
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.features import Features
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class SqliteTableOptions(TableOptions):
    """How SQLite stores a model's table.

    Attributes:
        without_rowid: Store the rows in the primary key's own index instead of a separate rowid
            table (``WITHOUT ROWID``) - smaller and faster for a table looked up by a primary key
            that isn't a single integer. The table needs a primary key, and it can't be an
            ``AUTOINCREMENT`` one.
        strict: Create the table ``STRICT`` (SQLite 3.37+): a column holds values of its type
            only, and SQLite refuses a value it can't convert to it. Each column is declared with
            the ``STRICT`` type of its affinity - ``INTEGER``, ``REAL``, ``TEXT`` or ``BLOB``, and
            ``ANY`` for a numeric one (a date, a time).
    """

    dialect_name: ClassVar[str] = DialectName.SQLITE

    without_rowid: bool = False
    strict: bool = False

    def __post_init__(self) -> None:
        for option_name in ("without_rowid", "strict"):
            if type(getattr(self, option_name)) is not bool:
                raise ConfigurationError(
                    f"SqliteTableOptions {option_name} must be a bool, got {getattr(self, option_name)!r}"
                )

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects ``without_rowid`` for a table without a primary key or with one the database
        generates - a ``WITHOUT ROWID`` table stores its rows in the primary key's index and has no
        rowid to number rows with, so SQLite refuses both - and ``strict`` on a SQLite older than
        3.37.

        Args:
            model: The model.
            features: The features of the connection the table is created on.

        Raises:
            ConfigurationError: ``without_rowid`` is set and the model has no primary key, or a
                generated one.
            UnSupportedError: ``strict`` is set and the connection has no ``STRICT`` tables.
        """
        if self.without_rowid and not model._meta.has_primary_key:
            raise ConfigurationError(
                f"{model.__name__}: SqliteTableOptions(without_rowid=True) needs a primary key - the model "
                "declares Meta.primary_key = None."
            )
        primary_key_attribute = model._meta.primary_key_attribute
        pk_field = (
            model._meta.fields_map.get(primary_key_attribute) if isinstance(primary_key_attribute, str) else None
        )
        if self.without_rowid and pk_field is not None and pk_field.generated:
            raise ConfigurationError(
                f"{model.__name__}: SqliteTableOptions(without_rowid=True) needs a primary key the "
                f"database doesn't generate - declare {primary_key_attribute!r} with generated=False, or use another "
                "primary key."
            )
        if self.strict and not features.supports_strict_tables:
            minimum_version = ".".join(map(str, SQLITE_STRICT_SERVER_VERSION))
            raise UnSupportedError(
                f"{model.__name__}: SqliteTableOptions(strict=True) needs SQLite {minimum_version} or later - "
                "the connection's SQLite has no STRICT tables (features.supports_strict_tables)."
            )

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        options = []
        if self.strict:
            options.append(SQLITE_STRICT_TABLE_OPTION)
        if self.without_rowid:
            options.append(SQLITE_WITHOUT_ROWID_TABLE_OPTION)
        return f" {', '.join(options)}" if options else ""

    def get_column_type(self, column_type: str) -> str:
        """The ``STRICT`` type of a column's affinity in a strict table; the declared type
        otherwise."""
        if not self.strict:
            return column_type
        # Local import: the introspector reads into hare's models, whose modules import this one.
        from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector

        return SQLITE_STRICT_COLUMN_TYPES[SqliteIntrospector.get_type_affinity(column_type)]
