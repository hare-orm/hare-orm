from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from hare.query.statements.awaitable_query import AwaitableQuery


class PlanCoverage:
    """Counts the queries of a test run made to run and those of them built in full without keeping a
    plan - a property of each query's shape alone, whatever plans other tests left in the cache.
    Installed with plan verification; the counts of every worker are summed."""

    #: The queries made to run.
    made: ClassVar[int] = 0
    #: Those built in full that keep no plan.
    built_without_plan: ClassVar[int] = 0
    #: The unpatched ``_make_query_to_run()``, None while not installed.
    original: ClassVar[Callable[[AwaitableQuery[Any]], None] | None] = None

    @classmethod
    def install(cls) -> None:
        """Wraps ``AwaitableQuery._make_query_to_run()`` to count."""
        if cls.original is not None:
            return
        original = cls.original = AwaitableQuery._make_query_to_run

        def make_query_to_run(query: AwaitableQuery[Any]) -> None:
            original(query)
            cls.made += 1
            if query._statement_plan is None and query._deferred_plan_record is None:
                cls.built_without_plan += 1

        AwaitableQuery._make_query_to_run = make_query_to_run  # type: ignore[method-assign]

    @classmethod
    def get_summary(cls, made: int, built_without_plan: int) -> str:
        """The line reporting the counts.

        Args:
            made: The queries made to run.
            built_without_plan: Those built in full without keeping a plan.

        Returns:
            The line.
        """
        return f"PLAN COVERAGE: {built_without_plan} of {made} queries built in full without keeping a plan"
