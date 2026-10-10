from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.query_connection import QueryConnection
from hare.query.statements.select.combined.combined_branches import CombinedBranches

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.select.combined_query import CombinedQuery


class CombinedPlanDescriptions:
    """The plan description of a set operation's branches - each one's structure as it is built in."""

    @staticmethod
    def get_branches_plan_description(query: CombinedQuery) -> PlanDescription | None:
        """Describes the branches of a set operation: each branch's structure as it is built in, with
        the connection it is pinned to, and its values.

        Args:
            query: The set operation.

        Returns:
            The description, None when a branch keeps no plan.
        """
        branch_structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for branch in query._branches:
            branch_description = CombinedBranches.get_prepared_branch(branch).get_plan_description(PlanContext.EMPTY)
            if branch_description is None:
                return None
            branch_structures.append(
                (QueryConnection.get_pinned_connection_name(branch), branch_description.structure)
            )
            values.extend(branch_description.values)
            if origins is not None:
                origins = PlanParts.get_origins(branch_description, origins)
        return PlanDescription(tuple(branch_structures), values, origins)
