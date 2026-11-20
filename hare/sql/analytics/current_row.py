from __future__ import annotations

from hare.sql.terms.functions.window_frame_edge import WindowFrameEdge


class CurrentRow(WindowFrameEdge):
    """The current row as an edge of a window frame."""

    structure_attributes = ()

    def __str__(self) -> str:
        return "CURRENT ROW"
