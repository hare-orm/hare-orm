"""The checks of a query running on the plan an earlier query of its structure kept."""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans


class StatementPlanChecks:
    """A query on a kept plan gives what the query built in full gives."""

    @staticmethod
    def full_build() -> Any:
        """Runs the queries of its block built in full, as they ran before plans."""
        return patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)

    @classmethod
    async def assert_a_later_query_runs_on_its_plan(
        cls, build: Callable[[Any], Awaitable[Any]], values: tuple[Any, Any], *, same_results: bool = False
    ) -> None:
        """A query of the structure of an earlier one runs on its plan - one plan hit - and gives the
        rows the query built in full gives.

        Args:
            build: Runs the query of a value.
            values: The value of the earlier query and of the later one.
            same_results: Whether both values give the same rows - else they must differ, so a plan
                bound to the earlier value shows.
        """
        first_value, second_value = values
        first = await build(first_value)
        with cls.full_build():
            expected = await build(second_value)
        assert same_results or expected != first
        hits = StatementPlans.hits
        assert await build(second_value) == expected
        assert StatementPlans.hits - hits == 1
