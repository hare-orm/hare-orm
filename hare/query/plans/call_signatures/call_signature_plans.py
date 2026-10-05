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
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.scopes.row_visibility import RowVisibility


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
        describes_json_containment_by_shape: bool,
    ) -> tuple[tuple[Any, ...], list[Any]] | None:
        """Describes the calls: each call with the structure of its filter values - a ``Q`` condition
        by its own description - and the values a plan binds.

        Args:
            call_signature: The queryset class, then the calls.
            call_values: The values of their filters, in call order.
            single_parameter_in_list_min_length: The dialect's length from which an ``__in`` list
                binds as one parameter.
            model: The model queried.
            describes_json_containment_by_shape: Whether the dialect's JSON containment SQL follows the
                value's keys and nesting.

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
                    context = PlanContext(
                        None,
                        single_parameter_in_list_min_length,
                        model=model,
                        describes_json_containment_by_shape=describes_json_containment_by_shape,
                    )
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
                    context = PlanContext(
                        None,
                        single_parameter_in_list_min_length,
                        model=model,
                        describes_json_containment_by_shape=describes_json_containment_by_shape,
                    )
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
                        context = PlanContext(
                            None,
                            single_parameter_in_list_min_length,
                            model=model,
                            describes_json_containment_by_shape=describes_json_containment_by_shape,
                        )
                    description = FilterPlanDescriptions.get_filter_value_plan_description(value, context)
                    if description is None:
                        return None
                    filter_structures.append(description.structure)
                    values += description.values
                    continue
                filter_structures.append(
                    FilterPlanDescriptions.describe_value_filter(
                        filter_key,
                        value,
                        values,
                        single_parameter_in_list_min_length,
                        describes_json_containment_by_shape,
                    )
                )
            structures.append((call, tuple(filter_structures)))
        return tuple(structures), values

    @classmethod
    def get_key(
        cls,
        query_class: type,
        specification: QuerySpecification[Any],
        connection: DatabaseClient,
        type_structure: Any,
    ) -> tuple[tuple[Any, ...], Sequence[Any], RowVisibility | None] | None:
        """The key of the plan a queryset's calls run on as ``query_class`` - the query class, the
        connection, the zone, the default scope, the calls and what else the query class keys by.

        Args:
            query_class: The class of the query run.
            specification: The queryset, or the query made from it.
            connection: The connection.
            type_structure: What else the query class keys by (``_get_call_signature_type_part()``).

        Returns:
            The key, the values bound - the default scope's, then the calls' - and the visibility the
            default scope was resolved with (None for a model without one); None when the default
            scope can't be resolved now (no tenant is active), or a condition keeps no plan.
        """
        row_scopes = RowScopes.of(specification.model)
        if row_scopes.scopes:
            visibility = specification._visibility.get_for_active_tenant()
            scope = row_scopes.get_filters_plan_description(
                visibility, uses_default_scope=specification._uses_default_scope
            )
            if scope is None:
                return None
            scope_description = scope[0]
        else:
            visibility = None
            scope_description = PlanDescription.EMPTY
        call_signature = specification._call_signature  # type: ignore[attr-defined]
        call_values = specification._call_values  # type: ignore[attr-defined]
        last_call = call_signature[-1]
        if last_call is not call_signature[0] and last_call[0] == "get":
            # get() alone - it starts its signature - of plain values, each bound as given: the call
            # holds their types, the signature is the structure.
            calls_structure = call_signature
        else:
            calls_description = cls.describe_calls(
                call_signature,
                call_values,
                connection.dialect.parameters.single_parameter_in_list_min_length,
                specification.model,
                connection.dialect.parameters.describes_json_containment_by_shape,
            )
            if calls_description is None:
                return None
            calls_structure, call_values = calls_description
        key = (
            "calls",
            query_class,
            connection.dialect,
            connection.connection_alias,
            Timezone.get_rendered_zone_name(),
            scope_description.structure,
            calls_structure,
            type_structure,
        )
        if not scope_description.values:
            return key, call_values, visibility
        return key, [*scope_description.values, *call_values], visibility

    @staticmethod
    def find(model: Any, key: tuple[Any, ...]) -> StatementPlan | None:
        """The plan kept under the key of a queryset's calls.

        Args:
            model: The model queried.
            key: The key (``get_key()``).

        Returns:
            The plan, None when none is kept yet.
        """
        found = StatementPlans.call_signature_plans.get_for_model(model, key)
        return None if found is None else found[0]

    @staticmethod
    def find_with_value_positions(
        model: Any, key: tuple[Any, ...]
    ) -> tuple[StatementPlan, tuple[int, ...] | None] | None:
        """The plan kept under the key of a queryset's calls, with the position of each value of its
        description among the values bound - the calls' values, then the query class's own.

        Args:
            model: The model queried.
            key: The key (``get_key()``).

        Returns:
            The plan and the positions - None when they come in the plan's order; None when no plan
            is kept yet.
        """
        return StatementPlans.call_signature_plans.get_for_model(model, key)

    @staticmethod
    def get_value_origins(specification: QuerySpecification[Any], connection: DatabaseClient) -> list[Any] | None:
        """The origin of each value ``get_key()`` gives - the default scope's, then the calls' - for
        a plan kept under the key: the plan's values are found among them by their origins.

        Args:
            specification: The query of the calls, not built yet.
            connection: The connection.

        Returns:
            The origins, None when a value's origin isn't known.
        """
        origins: list[Any] = []
        row_scopes = RowScopes.of(specification.model)
        if row_scopes.scopes:
            scope = row_scopes.get_filters_plan_description(
                specification._visibility.get_for_active_tenant(), uses_default_scope=specification._uses_default_scope
            )
            if scope is None:
                return None
            # The default scope's conditions name the query they are put in (_apply_ambient_scope()).
            origins += [PlanOrigins.get_value_origin(specification, scope_key) for scope_key in scope[1]]
        single_parameter_in_list_min_length = connection.dialect.parameters.single_parameter_in_list_min_length
        describes_json_containment_by_shape = connection.dialect.parameters.describes_json_containment_by_shape
        context = PlanContext(
            None,
            single_parameter_in_list_min_length,
            model=specification.model,
            describes_json_containment_by_shape=describes_json_containment_by_shape,
        )
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
                    filter_key, value, values, single_parameter_in_list_min_length, describes_json_containment_by_shape
                )
                origins += [(token, filter_key)] * len(values)
        return origins

    @staticmethod
    def record(
        model: Any,
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
            model: The model queried.
            key: The key (``get_key()``).
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
        StatementPlans.call_signature_plans[(model, *key)] = (
            plan,
            None if positions == list(range(len(positions))) else tuple(positions),
        )
