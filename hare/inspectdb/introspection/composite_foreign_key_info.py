from __future__ import annotations

from dataclasses import dataclass

from hare.fields.enums import OnDelete
from hare.inspectdb.constants import FOREIGN_KEY_NO_ACTION


@dataclass
class CompositeForeignKeyInfo:
    """A composite FOREIGN KEY referencing the target's whole primary key under the key column naming
    of a relation - rendered as one ``ForeignKeyField``.
    """

    #: Python attribute name for the generated ForeignKeyField - chosen so that
    #: ``f"{field_name}_{target_pk_component}"`` reproduces every entry in ``columns`` exactly.
    field_name: str
    #: This table's own local columns, reordered to match the target's declared primary key
    #: column order (not necessarily the FK constraint's own written order).
    columns: tuple[str, ...]
    target_table: str
    on_delete: OnDelete = OnDelete.CASCADE
    #: The ON UPDATE action, as the database spells it.
    on_update: str = FOREIGN_KEY_NO_ACTION
    #: ``DEFERRABLE INITIALLY IMMEDIATE``/``DEFERRABLE INITIALLY DEFERRED``, ``""`` for a constraint
    #: checked at once - None where the database doesn't tell (SQLite).
    deferral: str | None = None
