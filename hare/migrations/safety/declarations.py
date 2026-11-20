from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TableRowEstimate:
    """Rows of one physical table across moves; recreated tables receive a new estimate."""

    original_table: tuple[str | None, str] | None
    row_count: int | None = None
    count_pending: bool = False
