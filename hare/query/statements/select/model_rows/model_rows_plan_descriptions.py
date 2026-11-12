from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.subqueries.outer_scope import OuterScope
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.statements.select.model_rows.select_related_joins import SelectRelatedJoins

if TYPE_CHECKING:
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
        return StatementPlanDescriptions.get_join_conditions_plan_description(query.model, paths_and_conditions)

    @staticmethod
    def describe_queryset(
        query: ModelRowsQuery[Any], *, plain: bool, built_into_another: bool = False
    ) -> PlanDescription | None:
        """Describe the selected statement form without looking up its plan.

        The caller selects the form once and retains its validation order. Origin recording
        reuses this description after the initial lookup has accepted the query state.
        """
        return (
            query._describe_plain_statement(built_into_another)
            if plain
            else query._describe_statement(built_into_another)
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
            description = ModelRowsPlanDescriptions.describe_queryset(
                query, plain=True, built_into_another=built_into_another
            )
            if description is None:
                return None, None
            # A plan kept under the key is the answer to whether the query keeps one - except
            # inside a correlated subquery, where that also depends on the enclosing query.
            if not built_into_another and OuterScope.current_expression_context.get() is None:
                plan = StatementPlans.find(query.model, description.structure)
            if plan is None and not StatementPlanDescriptions.query_state_is_plannable(query):
                return None, None
            return description, plan
        if not StatementPlanDescriptions.query_state_is_plannable(query):
            return None, None
        description = ModelRowsPlanDescriptions.describe_queryset(
            query, plain=False, built_into_another=built_into_another
        )
        if description is None:
            return None, None
        if not built_into_another:
            plan = StatementPlans.find(query.model, description.structure)
        return description, plan
