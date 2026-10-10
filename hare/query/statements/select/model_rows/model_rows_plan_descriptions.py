from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.subqueries.outer_query_state import outer_expression_context
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.statements.select.model_rows.select_related_joins import SelectRelatedJoins

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.select.model_rows_query import ModelRowsQuery


class ModelRowsPlanDescriptions:
    """The plan description of a query of model instances: a plain query takes the short key, any other
    one the full description, with the extra conditions of select_related() in join order."""

    @staticmethod
    def has_plain_query_state(query: ModelRowsQuery[Any]) -> bool:
        """Whether the query selects every column of its model and has only filters, ordering, a plain
        ``.distinct()`` and a slice - it then takes the short plan key.

        Args:
            query: The model rows query.

        Returns:
            True for a plain query.
        """
        if query._select_related or query._effective_fields_for_select or query._annotations:
            return False
        options = query._options
        return options is QueryOptions.DEFAULT or not (
            options.select_related_extra_conditions
            or options.cursor_values
            or options.before_cursor_values
            or options.distinct_on
            or options.select_for_update
        )

    @staticmethod
    def get_extra_conditions_plan_description(query: ModelRowsQuery[Any]) -> PlanDescription | None:
        """Describes the ``Select(relation, extra_condition=Q(...))`` conditions in join order -
        two queries selecting the same relations with other conditions never share a plan.

        Args:
            query: The model rows query.

        Returns:
            The description, None when a condition keeps no plan.
        """
        paths_and_conditions = SelectRelatedJoins.select_related_extra_conditions_in_join_order(query)
        if not paths_and_conditions:
            return PlanDescription((), [])
        conditions_description = StatementPlanDescriptions.get_conditions_plan_description(
            [extra_condition for _path, extra_condition in paths_and_conditions], PlanContext.EMPTY
        )
        if conditions_description is None:
            return None
        return PlanDescription(
            (tuple(path for path, _extra_condition in paths_and_conditions), conditions_description.structure),
            conditions_description.values,
            # Bound into each JOIN the conditions are folded into - none of a relation the query
            # doesn't cross.
            PlanParts.get_optional_origins(conditions_description, []) if PlanOrigins.records else None,
        )

    @staticmethod
    def get_queryset_plan_description(
        query: ModelRowsQuery[Any], built_into_another: bool = False
    ) -> tuple[PlanDescription | None, StatementPlan | None]:
        """Describes this query - the structure is the plan key - and finds the plan under it. A plain
        query takes the short key of ``plain_plan_slots`` and its plan is looked up first; any other
        one the key of ``plan_slots``.

        Args:
            query: The model rows query.
            built_into_another: Whether the query is built into another one: the key leaves out the
                dialect, and no plan is looked up.

        Returns:
            The description, None for a query that keeps no plan, and the plan found.
        """
        plan: StatementPlan | None = None
        if ModelRowsPlanDescriptions.has_plain_query_state(query):
            description = query._describe_plain_statement(built_into_another)
            if description is None:
                return None, None
            # A plan kept under the key is the answer to whether the query keeps one - except
            # inside a correlated subquery, where that also depends on the enclosing query.
            if not built_into_another and outer_expression_context.get() is None:
                plan = StatementPlans.find_for_model(query.model, description.structure)
            if plan is None and not StatementPlanDescriptions.query_state_is_plannable(query):
                return None, None
            return description, plan
        if not StatementPlanDescriptions.query_state_is_plannable(query):
            return None, None
        description = query._describe_statement(built_into_another)
        if description is None:
            return None, None
        if not built_into_another:
            plan = StatementPlans.find_for_model(query.model, description.structure)
        return description, plan
