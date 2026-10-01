from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.cache import Cache
from hare.query.constants import CALL_SIGNATURE_FILTER_CALLS
from hare.query.expressions import Q
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement_plans import StatementPlans
from hare.query.scopes.row_scopes import RowScopes
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.plans.statement_plan import StatementPlan
    from hare.query.queryset.query_spec import QuerySpec
    from hare.query.scopes.row_visibility import RowVisibility


class CallSignaturePlans:
    """The plans of querysets made by simple calls alone (``QuerySet._call_signature``), kept under
    the key of the calls: a query of such a queryset runs on its plan without being built, not
    even in part."""

    @staticmethod
    def describe_calls(
        call_signature: tuple[Any, ...], call_values: tuple[Any, ...], single_parameter_in_list_min_length: int | None
    ) -> tuple[tuple[Any, ...], list[Any], list[str]]:
        """Describes the calls: each call with the structure of its filter values, the values a plan
        binds, and the filter key of each value.

        Args:
            call_signature: The calls.
            call_values: The values of their filters, in call order.
            single_parameter_in_list_min_length: The dialect's length from which an ``__in`` list
                binds as one parameter.

        Returns:
            The structure, the values and their keys.
        """
        values_of_calls = iter(call_values)
        values: list[Any] = []
        value_keys: list[str] = []
        structures: list[Any] = []
        for call in call_signature:
            if call[0] not in CALL_SIGNATURE_FILTER_CALLS:
                structures.append(call)
                continue
            filter_structures = []
            for filter_key in call[1]:
                value_count = len(values)
                filter_structures.append(
                    Q.describe_value_filter(
                        filter_key, next(values_of_calls), values, single_parameter_in_list_min_length
                    )
                )
                value_keys += [filter_key] * (len(values) - value_count)
            structures.append((call, tuple(filter_structures)))
        return tuple(structures), values, value_keys

    @classmethod
    def get_key(
        cls, query_class: type, spec: QuerySpec[Any], db: DatabaseClient, kind_structure: Any
    ) -> tuple[tuple[Any, ...], list[Any], tuple[str, ...], RowVisibility] | None:
        """The key of the plan a queryset's calls run on as ``query_class`` - the query class, the
        connection, the zone, the default scope, the calls and what else the query class keys by.

        Args:
            query_class: The class of the query run.
            spec: The queryset, or the query made from it.
            db: The connection.
            kind_structure: What else the query class keys by (``_get_call_signature_kind_part()``).

        Returns:
            The key, the values bound - the default scope's, then the calls' - their keys, and the
            visibility the default scope was resolved with; None when the default scope can't be
            resolved now (no tenant is active).
        """
        visibility = spec._visibility.get_for_active_tenant()
        row_scopes = RowScopes.of(spec.model)
        if row_scopes.scopes:
            scope = row_scopes.get_filters_plan_description(visibility, uses_default_scope=spec._uses_default_scope)
            if scope is None:
                return None
            scope_description, scope_keys = scope
        else:
            scope_description, scope_keys = PlanDescription.EMPTY, ()
        calls_structure, call_values, call_value_keys = cls.describe_calls(
            spec._call_signature,  # type: ignore[attr-defined]
            spec._call_values,  # type: ignore[attr-defined]
            db.dialect.single_parameter_in_list_min_length,
        )
        key = (
            "calls",
            query_class,
            db.dialect,
            db.connection_name,
            Timezone.get_rendered_zone_name(),
            scope_description.structure,
            calls_structure,
            kind_structure,
        )
        return key, [*scope_description.values, *call_values], (*scope_keys, *call_value_keys), visibility

    @staticmethod
    def find(model: Any, key: tuple[Any, ...]) -> StatementPlan | None:
        """The plan kept under the key of a queryset's calls.

        Args:
            model: The model queried.
            key: The key (``get_key()``).

        Returns:
            The plan, None when none is kept yet.
        """
        split_plan_key = StatementPlans.call_signature_plans.get_for_model(model, key)
        if split_plan_key is None:
            return None
        plan_model, model_index, rest = split_plan_key
        if plan_model is None:
            return StatementPlans.plans.get(rest)
        return StatementPlans.plans.get_for_model(plan_model, rest, index=model_index)

    @staticmethod
    def record(
        model: Any,
        key: tuple[Any, ...],
        value_keys: tuple[str, ...],
        kind_value_count: int,
        plan: StatementPlan,
        plan_key: tuple[Any, ...],
    ) -> None:
        """Keeps a plan found or built for a query of a queryset's calls under their key too, when
        its values are the calls' own, in their order.

        Args:
            model: The model queried.
            key: The key (``get_key()``).
            value_keys: The keys of the values of the default scope and the calls.
            kind_value_count: How many values the query class binds after them.
            plan: The plan.
            plan_key: The key the plan is kept under in ``StatementPlans.plans``.
        """
        if plan.sql is None:
            return
        plan_keys = [value_key for value_key, _ref in plan.value_refs]
        join_condition_value_count = sum(join_condition.value_count for join_condition in plan.join_conditions)
        if (
            plan_keys[: len(value_keys)] == list(value_keys)
            and len(plan_keys) == len(value_keys) + kind_value_count + join_condition_value_count
        ):
            StatementPlans.call_signature_plans[(model, *key)] = Cache.split(plan_key)
