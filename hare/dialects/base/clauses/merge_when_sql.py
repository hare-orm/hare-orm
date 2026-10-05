from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.base.clauses.enums import MergeAction, MergeMatch


@dataclass(frozen=True, slots=True)
class MergeWhenSql:
    """One ``WHEN`` branch of a ``MERGE``, its parts rendered.

    Attributes:
        match: Which rows it takes.
        action: What it does with them.
        condition_sql: The condition after ``AND``, None for none.
        columns_sql: The quoted columns an ``UPDATE`` sets or an ``INSERT`` writes.
        values_sql: The value of each column, in the same order.
    """

    match: MergeMatch
    action: MergeAction
    condition_sql: str | None = None
    columns_sql: tuple[str, ...] = ()
    values_sql: tuple[str, ...] = ()
