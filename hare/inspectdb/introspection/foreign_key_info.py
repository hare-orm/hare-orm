from __future__ import annotations

from dataclasses import dataclass

from hare.fields.enums import OnDelete
from hare.inspectdb.constants import FOREIGN_KEY_NO_ACTION


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
    #: The schema of the target table - None where the database has one namespace of tables.
    target_schema: str | None = None
    #: The ON UPDATE action, as the database spells it.
    on_update: str = FOREIGN_KEY_NO_ACTION
    #: ``DEFERRABLE INITIALLY IMMEDIATE``/``DEFERRABLE INITIALLY DEFERRED``, ``""`` for a constraint
    #: checked at once - None where the database doesn't tell.
    deferral: str | None = None
