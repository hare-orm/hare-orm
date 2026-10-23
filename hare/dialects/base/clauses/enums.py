from __future__ import annotations

from enum import StrEnum


class RowLockStrength(StrEnum):
    """How strongly ``select_for_update()`` locks the rows it reads."""

    #: Against every other lock - the rows are about to change.
    UPDATE = "update"
    #: Against updates and deletes, letting rows referencing them be written - the key stays.
    NO_KEY_UPDATE = "no_key_update"
    #: Against updates and deletes; other readers may lock the rows the same way.
    SHARE = "share"
    #: Against deletes and key updates only - the rows may change otherwise.
    KEY_SHARE = "key_share"


class MergeMatch(StrEnum):
    """Which rows a ``WHEN`` branch of a ``MERGE`` takes."""

    #: A target row the source row matches.
    MATCHED = "matched"
    #: A source row no target row matches.
    NOT_MATCHED = "not_matched"
    #: A target row no source row matches.
    NOT_MATCHED_BY_SOURCE = "not_matched_by_source"


class MergeAction(StrEnum):
    """What a ``WHEN`` branch of a ``MERGE`` does with its rows."""

    UPDATE = "update"
    DELETE = "delete"
    INSERT = "insert"
    #: Leaves the row alone - and keeps the later branches from taking it.
    DO_NOTHING = "do_nothing"
