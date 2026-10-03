from __future__ import annotations

from enum import StrEnum


class RowOperation(StrEnum):
    """What a write did to the rows of a model."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
