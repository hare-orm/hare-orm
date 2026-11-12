from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

if TYPE_CHECKING:
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.rows.row_layout import RowLayout
    from hare.query.rows.values_rows.values_rows import ValuesRows
    from hare.query.scopes.row_visibility import RowVisibility


class ParameterBinder(Protocol):
    """The prepared parameter target, including its optional rendering constraint."""

    def __call__(self, parameters: list[Any], value: Any, always_parameterized: bool = False) -> bool: ...


class RenderedStatement(NamedTuple):
    """SQL and parameters of the current build, independently of whether it keeps a plan."""

    sql: str
    parameters: list[Any]


class ValuesReading(NamedTuple):
    """A result shape's reader and logical grouping, separate from a shared SQL plan."""

    reader: ValuesRows
    converters: Sequence[tuple[Any, Callable[[Any], Any] | None]]
    layout: RowLayout | None


class CallPlanHit(NamedTuple):
    """A query found its plan - all a run on it needs, none of it kept on the query."""

    #: The plan.
    plan: StatementPlan
    #: The SQL text of the plan - never None on a hit.
    sql: str
    #: The parameters bound for this execution.
    parameters: list[Any]
    #: What the query class reads the result with, kept with the plan; None for a plan found by the
    #: description's key.
    reading: Any
    #: The visibility the default scope was taken with for this execution.
    visibility: RowVisibility


class CallPlanLookup(NamedTuple):
    """An unsuccessful lookup, carrying only what a subsequent build records."""

    #: The key a plan built now is kept under too - None when there is no key, or a plan is kept under
    #: it already and this execution's values didn't fit it.
    record_key: tuple[Any, ...] | None = None
    #: How many values the query class binds after the calls' own.
    type_value_count: int = 0
