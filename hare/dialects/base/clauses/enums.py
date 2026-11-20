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


class JoinType(StrEnum):
    """How a source is joined - the dialect's clauses write the keyword of each."""

    INNER = "inner"
    LEFT_OUTER = "left_outer"
    #: Each row joined with the one row of the other table closest to it by an inequality, after
    #: the equalities - or with NULLs when none is.
    ASOF_LEFT = "asof_left"
    #: Each row repeated with each element of an array of it - no row for an empty array, or (left) a
    #: row with the element type's default.
    ARRAY = "array"
    LEFT_ARRAY = "left_array"
