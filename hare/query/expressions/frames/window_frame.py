from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.enums import WindowFrameUnit
from hare.query.expressions.frames.constants import MAX_WINDOW_FRAME_OFFSET
from hare.sql.analytics.current_row import CurrentRow
from hare.sql.analytics.declarations import Following, Preceding

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class WindowFrame:
    """The rows a window function computes over, around the current row - ``Window(...,
    frame=RowRange(start=-2, end=0))``. ``start`` and ``end`` are offsets from the current row: a
    negative number before it (``PRECEDING``), 0 the current row, a positive number after it
    (``FOLLOWING``), None unbounded - from the partition's first row for ``start``, to its last for
    ``end``.

    Args:
        start: Where the frame starts.
        end: Where it ends.

    Raises:
        QueryError: An offset isn't None or an int within ``MAX_WINDOW_FRAME_OFFSET``, or ``start`` comes
            after ``end``.

    Attributes:
        frame_type: The frame's SQL unit - ``ROWS``, ``RANGE``.
        offsets_need_one_ordering: An offset other than 0 measures the ordering's value, so the
            window must be ordered by exactly one field.
    """

    frame_type: ClassVar[WindowFrameUnit]
    offsets_need_one_ordering: ClassVar[bool] = False

    def __init__(self, start: int | None = None, end: int | None = None) -> None:
        for name, offset in (("start", start), ("end", end)):
            if offset is not None and (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or not -MAX_WINDOW_FRAME_OFFSET <= offset <= MAX_WINDOW_FRAME_OFFSET
            ):
                raise QueryError(
                    f"{type(self).__name__}({name}=...) takes None or an int from {-MAX_WINDOW_FRAME_OFFSET} to "
                    f"{MAX_WINDOW_FRAME_OFFSET}, got {offset!r}"
                )
        if start is not None and end is not None and start > end:
            raise QueryError(f"{type(self).__name__}: start ({start}) comes after end ({end})")
        self.start = start
        self.end = end

    def has_offsets(self) -> bool:
        """Whether an edge is a number of rows or values away from the current row - neither
        unbounded nor the current row."""
        return any(offset not in {None, 0} for offset in (self.start, self.end))

    @staticmethod
    def get_edge(offset: int | None, unbounded: type[Preceding] | type[Following]) -> Any:
        """The SQL edge of an offset.

        Args:
            offset: The offset.
            unbounded: The edge an unbounded offset is.

        Returns:
            ``UNBOUNDED PRECEDING``/``FOLLOWING``, ``n PRECEDING``, ``CURRENT ROW`` or ``n FOLLOWING``.
        """
        if offset is None:
            return unbounded()
        if offset < 0:
            return Preceding(-offset)
        if offset == 0:
            return CurrentRow()
        return Following(offset)

    def apply(self, term: WindowFrameAnalyticFunction) -> WindowFrameAnalyticFunction:
        """Gives a window function term the frame.

        Args:
            term: The term.

        Returns:
            The term framed.
        """
        start_edge = self.get_edge(self.start, Preceding)
        end_edge = self.get_edge(self.end, Following)
        if self.frame_type is WindowFrameUnit.RANGE:
            return term.range(start_edge, end_edge)
        return term.rows(start_edge, end_edge)

    def get_plan_key(self) -> tuple[Any, ...]:
        """What tells the frame apart in a query plan's key."""
        return (type(self), self.start, self.end)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(start={self.start!r}, end={self.end!r})"

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and other.start == self.start and other.end == self.end  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash(self.get_plan_key())
