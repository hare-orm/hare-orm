"""The checks of a query running on the plan an earlier query of its structure kept."""

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from hare.models import Model
from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans


class StatementPlanChecks:
    """A query on a kept plan gives what the query built in full gives."""

    @staticmethod
    @contextmanager
    def assert_decode_plans_reused(*models: type[Model]) -> Generator[None, None, None]:
        """Require populated model caches and preserve each plan's identity across a repeated read."""
        previous = []
        for model in models:
            bucket = StatementPlans.decode_plans.get_bucket(model)
            assert bucket, f"{model.__name__} has no decode plans to reuse"
            previous.append((bucket, dict(bucket)))
        yield
        for bucket, plans in previous:
            assert bucket.keys() == plans.keys()
            assert all(bucket[key] is plan for key, plan in plans.items())

    @staticmethod
    @contextmanager
    def full_build() -> Generator[None, None, None]:
        """Runs the queries of its block built in full, as they ran before plans."""
        with (
            patch.object(StatementPlanRuns, "find_plan", return_value=None),
            patch.object(CallSignaturePlans, "find_with_value_positions", return_value=None),
        ):
            yield

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
