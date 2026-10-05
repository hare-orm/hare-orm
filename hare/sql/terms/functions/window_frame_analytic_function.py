from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

from hare.sql.terms.functions.analytic_function import AnalyticFunction

EdgeT = TypeVar("EdgeT", bound="WindowFrameAnalyticFunction.Edge")


class WindowFrameAnalyticFunction(AnalyticFunction):
    """A window function with a frame - ``ROWS`` or ``RANGE`` between two edges."""

    class Edge:
        def __init__(self, value: str | int | None = None) -> None:
            self.value = value

        def __str__(self) -> str:
            # Only None is unbounded - 0 PRECEDING is the current row.
            return "{value} {modifier}".format(
                value=self.value if self.value is not None else "UNBOUNDED",
                modifier=self.modifier,  # type:ignore[attr-defined]
            )

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(name, *args, **kwargs)
        self.frame: str | None = None
        self.bound: Any = None

    def _set_frame_and_bounds(self, frame: str, bound: str | EdgeT, and_bound: EdgeT | None) -> None:
        if self.frame or self.bound:
            raise AttributeError()

        self.frame = frame
        self.bound = (bound, and_bound) if and_bound else bound

    @BuilderMethods.builder
    def rows(self, bound: str | EdgeT, and_bound: EdgeT | None = None) -> Self:
        self._set_frame_and_bounds("ROWS", bound, and_bound)
        return self

    @BuilderMethods.builder
    def range(self, bound: str | EdgeT, and_bound: EdgeT | None = None) -> Self:
        self._set_frame_and_bounds("RANGE", bound, and_bound)
        return self

    def _orderby_field(self, field: Any, orient: Any, sql_context: SqlContext) -> str:
        if self.has_value_offsets():
            field = sql_context.dialect.renderers.get_value_range_ordering_term(field)
        return super()._orderby_field(field, orient, sql_context)

    def has_value_offsets(self) -> bool:
        """Whether the frame is a ``RANGE`` with an edge a number of values away from the current
        row's."""
        if self.frame != "RANGE":
            return False
        edges = self.bound if isinstance(self.bound, tuple) else (self.bound,)
        return any(isinstance(edge, WindowFrameAnalyticFunction.Edge) and edge.value is not None for edge in edges)

    def get_frame_sql(self) -> str:
        if not isinstance(self.bound, tuple):
            return f"{self.frame} {self.bound}"

        lower, upper = self.bound
        return f"{self.frame} BETWEEN {lower} AND {upper}"

    def get_partition_sql(self, sql_context: SqlContext) -> str:
        partition_sql = super().get_partition_sql(sql_context)

        if not self.frame and not self.bound:
            return partition_sql

        return f"{partition_sql} {self.get_frame_sql()}"
