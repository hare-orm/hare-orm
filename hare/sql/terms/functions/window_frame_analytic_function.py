from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.sql.context import SqlContext
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

from hare.sql.terms.functions.analytic_function import AnalyticFunction

EdgeT = TypeVar("EdgeT", bound="WindowFrameAnalyticFunction.Edge")


class WindowFrameAnalyticFunction(AnalyticFunction):
    class Edge:
        def __init__(self, value: str | int | None = None) -> None:
            self.value = value

        def __str__(self) -> str:
            # pylint: disable=E1101
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

    @builder
    def rows(  # type:ignore[return]
        self, bound: str | EdgeT, and_bound: EdgeT | None = None
    ) -> Self:
        self._set_frame_and_bounds("ROWS", bound, and_bound)

    @builder
    def range(  # type:ignore[return]
        self, bound: str | EdgeT, and_bound: EdgeT | None = None
    ) -> Self:
        self._set_frame_and_bounds("RANGE", bound, and_bound)

    def get_frame_sql(self) -> str:
        if not isinstance(self.bound, tuple):
            return f"{self.frame} {self.bound}"

        lower, upper = self.bound
        return f"{self.frame} BETWEEN {lower} AND {upper}"

    def get_partition_sql(self, ctx: SqlContext) -> str:
        partition_sql = super().get_partition_sql(ctx)

        if not self.frame and not self.bound:
            return partition_sql

        return f"{partition_sql} {self.get_frame_sql()}"
