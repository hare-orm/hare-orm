from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from hare.ddl.table_options import TableOptions
from hare.dialects.enums import DialectName
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class SqliteTableOptions(TableOptions):
    """How SQLite stores a model's table.

    Attributes:
        without_rowid: Store the rows in the primary key's own index instead of a separate rowid
            table (``WITHOUT ROWID``) - smaller and faster for a table looked up by a primary key
            that isn't a single integer. The table needs a primary key, and it can't be an
            ``AUTOINCREMENT`` one.
    """

    dialect_name: ClassVar[str] = DialectName.SQLITE

    without_rowid: bool = False

    def raise_if_unsupported(self, model: type[Model]) -> None:
        """Rejects ``without_rowid`` for a table without a primary key or with one the database
        generates - a ``WITHOUT ROWID`` table stores its rows in the primary key's index and has no
        rowid to number rows with, so SQLite refuses both.

        Args:
            model: The model.

        Raises:
            ConfigurationError: ``without_rowid`` is set and the model has no primary key, or a
                generated one.
        """
        if self.without_rowid and not model._meta.has_primary_key:
            raise ConfigurationError(
                f"{model.__name__}: SqliteTableOptions(without_rowid=True) needs a primary key - the model "
                "declares Meta.primary_key = None."
            )
        pk_attr = model._meta.pk_attr
        pk_field = model._meta.fields_map.get(pk_attr) if isinstance(pk_attr, str) else None
        if self.without_rowid and pk_field is not None and pk_field.generated:
            raise ConfigurationError(
                f"{model.__name__}: SqliteTableOptions(without_rowid=True) needs a primary key the "
                f"database doesn't generate - declare {pk_attr!r} with generated=False, or use another "
                "primary key."
            )

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        return " WITHOUT ROWID" if self.without_rowid else ""
