from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.fields.field import Field
from hare.models import Model


@dataclass
class ReferencingKeyColumns:
    """Relation key columns storing the values of another model's column.

    Attributes:
        model: The model declaring the relation.
        relation_field: The FK/O2O field, or the M2M field owning an automatic through table.
        qualified_table: The already-quoted table holding the key columns.
        key_columns: Each key column's name paired with its new SQL type.
    """

    model: type[Model]
    relation_field: Field[Any]
    qualified_table: str
    key_columns: tuple[tuple[str, str], ...]
