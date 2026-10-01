from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.enums import OnDelete

if TYPE_CHECKING:  # pragma: nocoverage
    pass


@dataclass
class ForeignKeyInfo:
    column: str
    target_table: str
    target_column: str
    #: An OnDelete value. PROTECT is NO ACTION in the database - reconstructed as NO ACTION.
    on_delete: OnDelete = OnDelete.CASCADE
    #: The target column when it isn't the target's single-column primary key - rendered as
    #: to_field=.
    to_field: str | None = None
    #: Postgres schema of the target table - None on SQLite, which has one namespace.
    target_schema: str | None = None
