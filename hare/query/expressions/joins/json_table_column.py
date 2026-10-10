from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.fields.field import Field


@dataclass(frozen=True)
class JsonTableColumn:
    """A column of a ``JsonTable`` - the value at ``path`` of each item, read as ``field`` reads it.

    Args:
        name: The column's name - ``<table name>__<name>`` reads it.
        field: The field of its type.
        path: The JSON path of the value in the item.
    """

    name: str
    field: Field[Any]
    path: str
