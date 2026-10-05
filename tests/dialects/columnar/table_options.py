from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from hare.ddl.table_options import TableOptions
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.features import Features
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class ColumnarTableOptions(TableOptions):
    """How the columnar dialect stores a model's table.

    Attributes:
        strict: Reject a value of another type than its column's (``STRICT``) - every column type
            must then be INTEGER, REAL, TEXT, BLOB or ANY.
        without_rowid: Store the rows in the primary key's own index (``WITHOUT ROWID``); needs a
            primary key the database doesn't generate.
    """

    dialect_name: ClassVar[str] = "columnar"

    strict: bool = False
    without_rowid: bool = False

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects ``without_rowid`` for a table without a primary key, or with one the database generates.

        Args:
            model: The model.

        Raises:
            ConfigurationError: ``without_rowid`` is set and the table can't be stored that way.
        """
        if not self.without_rowid:
            return
        primary_key_attribute = model._meta.primary_key_attribute
        pk_field = (
            model._meta.fields_map.get(primary_key_attribute) if isinstance(primary_key_attribute, str) else None
        )
        if not model._meta.has_primary_key or (pk_field is not None and pk_field.generated):
            raise ConfigurationError(
                f"{model.__name__}: ColumnarTableOptions(without_rowid=True) needs a primary key the "
                "database doesn't generate"
            )

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        options = [
            option for option, enabled in (("STRICT", self.strict), ("WITHOUT ROWID", self.without_rowid)) if enabled
        ]
        return f" {', '.join(options)}" if options else ""
