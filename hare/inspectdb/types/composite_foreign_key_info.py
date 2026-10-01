from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.enums import OnDelete

if TYPE_CHECKING:  # pragma: nocoverage
    pass


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
