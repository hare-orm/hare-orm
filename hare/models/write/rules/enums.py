from __future__ import annotations

from enum import StrEnum


class WriteType(StrEnum):
    """What a write does to its rows - the fields it may name and the values it writes itself follow from it."""

    #: New rows - the primary key, the version and the soft-delete field may be given.
    INSERT = "insert"
    #: Rows changed by the caller's values - the version is bumped, the soft-delete field left to
    #: ``delete()``/``restore()``.
    UPDATE = "update"
    #: The rows an insert conflicts with, updated with the inserted values - an update that can't move a row
    #: into another tenant either.
    CONFLICT_UPDATE = "conflict_update"
    #: The soft delete or restore of rows - an update of ``Meta.soft_delete_field`` alone.
    SOFT_DELETE = "soft_delete"
