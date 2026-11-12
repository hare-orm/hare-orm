from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.query.plans.constants import (
    CALL_SIGNATURE_ANNOTATION_CALLS,
    CALL_SIGNATURE_CONDITION_FILTER_CALLS,
    CALL_SIGNATURE_FILTER_CALLS,
)
from hare.query.plans.description.filter_plan_descriptions import FilterPlanDescriptions
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.scopes.row_scopes import RowScopes

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.statements.awaitable_query import AwaitableQuery


class CallSignaturePlans:
    """The plans of querysets made by simple calls alone (``QuerySet._call_signature``), kept under
    the key of the calls: a query of such a queryset runs on its plan without being built, not
    even in part."""

    @staticmethod
    def describe_calls(
        call_signature: tuple[Any, ...],
        call_values: tuple[Any, ...],
        single_parameter_in_list_min_length: int | None,
        model: Any,
    ) -> tuple[tuple[Any, ...], list[Any]] | None:
        """Describes the calls: each call with the structure of its filter values - a ``Q`` condition
        by its own description - and the values a plan binds.

        Args:
            call_signature: The queryset class, then the calls.
            call_values: The values of their filters, in call order.
            single_parameter_in_list_min_length: The dialect's length from which an ``__in`` list
                binds as one parameter.
            model: The model queried.

        Returns:
            The structure and the values, None when a condition keeps no plan.
        """
        values_of_calls = iter(call_values)
        values: list[Any] = []
        structures: list[Any] = [call_signature[0]]
        context: PlanContext | None = None
        for call in call_signature[1:]:
            if call[0] in CALL_SIGNATURE_ANNOTATION_CALLS:
                if context is None:
                    context = PlanContext(None, single_parameter_in_list_min_length, model=model)
                annotation_structures = []
                for _key in call[1]:
                    description = next(values_of_calls).get_plan_description(context)
                    if description is None:
                        return None
                    annotation_structures.append(description.structure)
                    values += description.values
                structures.append((call, tuple(annotation_structures)))
                continue
            if call[0] not in CALL_SIGNATURE_FILTER_CALLS:
                structures.append(call)
                continue
            filter_structures = []
            if call[0] in CALL_SIGNATURE_CONDITION_FILTER_CALLS:
                # The Q conditions, before the kwargs.
                if context is None:
                    context = PlanContext(None, single_parameter_in_list_min_length, model=model)
                for _condition_number in range(call[2]):
                    description = next(values_of_calls).get_plan_description(context)
                    if description is None:
                        return None
                    filter_structures.append(description.structure)
                    values += description.values
            for filter_key in call[1]:
                value = next(values_of_calls)
                if isinstance(value, Plannable):
                    # An expression or a query compared with - described as a condition does.
                    if context is None:
                        context = PlanContext(None, single_parameter_in_list_min_length, model=model)
                    description = FilterPlanDescriptions.get_filter_value_plan_description(value, context)
                    if description is None:
                        return None
                    filter_structures.append(description.structure)
                    values += description.values
                    continue
                filter_structures.append(
                    FilterPlanDescriptions.describe_value_filter(
                        filter_key, value, values, single_parameter_in_list_min_length
                    )
                )
            structures.append((call, tuple(filter_structures)))
        return tuple(structures), values

    @staticmethod
    def find_with_value_positions(
        model: Any, key: tuple[Any, ...]
    ) -> tuple[StatementPlan, tuple[int, ...] | None, Any] | None:
        """The plan kept under the key of a queryset's calls, with the position of each value of its
        description among the values bound - the calls' values, then the query class's own - and
        what the query class reads the result of the plan with.

        Args:
            model: The model queried.
            key: The structural key of the calls and their execution settings.

        Returns:
            The plan, the positions - None when they come in the plan's order - and the reading
            (``AwaitableQuery._get_call_plan_reading()``); None when no plan is kept yet.
        """
        return StatementPlans.call_signature_plans.get_bucket(model).get(key)

    @staticmethod
    def get_value_origins(specification: QuerySpecification[Any], connection: DatabaseClient) -> list[Any] | None:
        """The origin of each value the call-plan lookup binds - the default scope's, then the calls' - for
        a plan kept under the key: the plan's values are found among them by their origins.

        Args:
            specification: The query of the calls, not built yet.
            connection: The connection.

        Returns:
            The origins, None when a value's origin isn't known.
        """
        origins: list[Any] = []
        row_scopes = RowScopes.of(specification.model)
        if row_scopes.field_scopes:
            scope = row_scopes.get_filters_plan_description(
                specification._visibility.get_for_active_tenant(), uses_default_scope=specification._uses_default_scope
            )
            if scope is None:
                return None
            # The default scope's conditions name the query they are put in (_apply_ambient_scope()).
            origins += [PlanOrigins.get_value_origin(specification, scope_key) for scope_key in scope[1]]
        single_parameter_in_list_min_length = connection.dialect.parameters.single_parameter_in_list_min_length
        context = PlanContext(None, single_parameter_in_list_min_length, model=specification.model)
        call_values = iter(specification._call_values)  # type: ignore[attr-defined]
        # One kept filter call per filter call of the signature, in their order.
        pending_filter_calls = iter(specification._pending_filter_calls)
        for call in specification._call_signature[1:]:  # type: ignore[attr-defined]
            if call[0] in CALL_SIGNATURE_ANNOTATION_CALLS:
                for _key in call[1]:
                    description = PlanOrigins.describe(partial(next(call_values).get_plan_description, context))
                    if description is None or description.origins is None:
                        return None
                    origins += description.origins
                continue
            if call[0] not in CALL_SIGNATURE_FILTER_CALLS:
                continue
            pending_filter_call = next(pending_filter_calls, None)
            if pending_filter_call is None:
                return None
            kwargs = pending_filter_call[3]
            if call[0] in CALL_SIGNATURE_CONDITION_FILTER_CALLS:
                for _condition_number in range(call[2]):
                    description = PlanOrigins.describe(partial(next(call_values).get_plan_description, context))
                    if description is None or description.origins is None:
                        return None
                    origins += description.origins
            # The conditions of the kwargs name the call's kwargs (PendingFilterCalls).
            token = PlanOrigins.get_token(kwargs)
            for filter_key in call[1]:
                value = next(call_values)
                if isinstance(value, Plannable):
                    description = PlanOrigins.describe(
                        partial(FilterPlanDescriptions.get_filter_value_plan_description, value, context)
                    )
                    if description is None or description.origins is None:
                        return None
                    origins += description.origins
                    continue
                values: list[Any] = []
                FilterPlanDescriptions.describe_value_filter(
                    filter_key, value, values, single_parameter_in_list_min_length
                )
                origins += [(token, filter_key)] * len(values)
        return origins

    @staticmethod
    def record(
        query: AwaitableQuery[Any],
        key: tuple[Any, ...],
        value_origins: Sequence[Any],
        type_value_count: int,
        plan: StatementPlan,
        description: PlanDescription | None,
    ) -> None:
        """Keeps a plan found or built for a query of a queryset's calls under their key too, when
        each value of its description but the query class's own is a value of the calls - found by
        its origin - and each value of the calls is one of them.

        Args:
            query: The prepared executor, used only during registration and never retained.
            key: The structural key of the calls and their execution settings.
            value_origins: The origins of the values of the default scope and the calls
                (``get_value_origins()``).
            type_value_count: How many values the query class binds after them.
            plan: The plan.
            description: The description of the query of the calls, with the origin of each value.
        """
        if plan.sql is None or description is None or description.origins is None:
            return
        position_by_origin = {origin: position for position, origin in enumerate(value_origins)}
        if len(position_by_origin) != len(value_origins):
            return
        # A position in the values bound: the calls' values, then the query class's own - each value
        # of the description is one of the calls' by its origin, else the query class's next one, in
        # whatever order the description lists them.
        positions = []
        type_value_number = 0
        for origin in description.origins:
            position = position_by_origin.get(origin)
            if position is None:
                position = len(value_origins) + type_value_number
                type_value_number += 1
            positions.append(position)
        if type_value_number != type_value_count or len(set(positions)) != len(value_origins) + type_value_count:
            # A value of the calls the plan doesn't bind, or a value of no known origin.
            return
        reading = query._get_call_plan_reading(plan)
        StatementPlans.call_signature_plans.get_bucket(query.model)[key] = (
            plan,
            None if positions == list(range(len(positions))) else tuple(positions),
            reading,
        )
