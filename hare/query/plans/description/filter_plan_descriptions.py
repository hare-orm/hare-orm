from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from hare.query.expressions.constants import (
    ISNULL_LOOKUP_SUFFIXES,
    JSON_CONTAINMENT_LOOKUP_SUFFIXES,
    JSON_FILTER_LOOKUP_SUFFIX,
    LIST_LOOKUP_SUFFIXES,
    LONG_IN_LIST_STRUCTURE,
    PLAIN_VALUE_TYPES,
    RANGE_LOOKUP_SUFFIX,
)
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.query_connection import QueryConnection


class FilterPlanDescriptions:
    """How a filter describes its value for a plan: a plain value or a list by its type or length (its
    values bound), a value written into the SQL text by itself (part of the key), and a query
    compared with by the query's own description."""

    @staticmethod
    def describe_value_filter(
        key: str,
        value: Any,
        values: list[Any],
        single_parameter_in_list_min_length: int | None = None,
        describes_json_containment_by_shape: bool = True,
    ) -> Any:
        """Describes a filter whose value is no part of a query - a plain value or a list.

        Args:
            key: The filter key.
            value: The filter value.
            values: The values the plan binds - the filter's own are appended.
            single_parameter_in_list_min_length: The length from which an ``__in`` list binds as one
                parameter, None to describe every list by its length.
            describes_json_containment_by_shape: Whether a JSON containment's SQL follows the value's
                keys and nesting - True where the dialect is unknown.

        Returns:
            The structure of the value, part of the plan key.
        """
        if FilterPlanDescriptions.is_constant_filter(key, value):
            return type(value), value
        if (
            describes_json_containment_by_shape
            and key.endswith(JSON_CONTAINMENT_LOOKUP_SUFFIXES)
            and isinstance(value, (dict, list, tuple))
            and (isinstance(value, dict) or any(isinstance(item, (dict, list, tuple)) for item in value))
        ):
            # A JSON value of containers - its keys and nesting decide the SQL text a dialect may
            # build from it.
            values.append(value)
            return FilterPlanDescriptions.get_value_shape(value)
        if isinstance(value, (list, tuple, set)):
            if key.endswith(LIST_LOOKUP_SUFFIXES):
                # An __in/__not_in list renders a parameter per value other than None - a None ORs
                # in an IS NULL test; a list of none but None is a constant condition binding
                # nothing.
                has_none = None in value
                bound_count = sum(item is not None for item in value) if has_none else len(value)
                if bound_count:
                    values.append(value)
                if (
                    single_parameter_in_list_min_length is not None
                    and bound_count >= single_parameter_in_list_min_length
                    # Key rows bind by their count.
                    and type(next(iter(value))) is not tuple
                ):
                    # One parameter whatever the length.
                    return LONG_IN_LIST_STRUCTURE, has_none
                return bound_count, has_none
            if FilterPlanDescriptions.is_range_filter(key, value):
                # A bound left None opens the range on its side - a plain comparison of the other
                # bound.
                values.append(value)
                bounds = cast("list[Any] | tuple[Any, ...]", value)
                return RANGE_LOOKUP_SUFFIX, (bounds[0] is None, bounds[1] is None)
            # A list of another length renders another number of parameters.
            values.append(value)
            return len(value)
        if (
            type(value) not in PLAIN_VALUE_TYPES
            and isinstance(value, dict)
            and key.endswith(JSON_FILTER_LOOKUP_SUFFIX)
        ):
            # A JSON filter: its path, operator and the type of its value decide the SQL text.
            values.append(value)
            return FilterPlanDescriptions.get_value_shape(value)
        # The lookup builds its criterion from a value of this type - a float or a Decimal is
        # cast, a date part is an integer.
        values.append(value)
        return type(value)

    @staticmethod
    def get_value_shape(value: Any) -> Any:
        """What of a value a lookup building its criterion from it in its own way may read - a
        dict's keys, a list's length, each leaf's type, whether a datetime has a time zone, a
        boolean (an ``__isnull`` flag) - all of it but the other leaves' values.

        Args:
            value: The value.

        Returns:
            The shape, hashable.
        """
        if isinstance(value, dict):
            return dict, tuple((key, FilterPlanDescriptions.get_value_shape(item)) for key, item in value.items())
        if isinstance(value, (list, tuple, set)):
            return type(value), tuple(FilterPlanDescriptions.get_value_shape(item) for item in value)
        if isinstance(value, datetime):
            return datetime, value.utcoffset() is not None
        if isinstance(value, bool):
            # An __isnull flag - rendered into the SQL text.
            return bool, value
        return type(value)

    @staticmethod
    def is_constant_filter(key: str, value: Any) -> bool:
        """Whether a filter's value is rendered into the SQL text rather than bound - a None (a
        NULL test, or a comparison with NULL), the boolean of an ``__isnull`` lookup, or a ``__range``
        open on both sides (a NOT NULL test). Its value is
        part of the plan key; the build records no reference for it.

        Args:
            key: The filter key.
            value: The filter value.

        Returns:
            True for such a filter.
        """
        value_type = type(value)
        return (
            value is None
            or (value_type is bool and key.endswith(ISNULL_LOOKUP_SUFFIXES))
            or (
                # Cheapest test first - an __in list's first item is rarely None.
                (value_type is tuple or value_type is list)
                and value
                and value[0] is None
                and len(value) == 2
                and value[1] is None
                and key.endswith(RANGE_LOOKUP_SUFFIX)
            )
        )

    @staticmethod
    def is_range_filter(key: str, value: Any) -> bool:
        """Whether a filter is a ``__range`` of two bounds.

        Args:
            key: The filter key.
            value: The filter value.

        Returns:
            True for such a filter.
        """
        return key.endswith(RANGE_LOOKUP_SUFFIX) and isinstance(value, (list, tuple)) and len(value) == 2

    @staticmethod
    def get_filter_value_plan_description(value: Plannable, context: PlanContext) -> PlanDescription | None:
        """Describes a filter value that is a part of a query: a query compared with (built as a
        subquery), an expression, or a ``RawSQL`` fragment.

        Args:
            value: The value.
            context: The context the filter is resolved in.

        Returns:
            The description, None for a value keeping no plan.
        """
        # Deferred import: hare.query.queryset imports this module at import time.
        from hare.query.queryset import QuerySet
        from hare.query.statements import AwaitableQuery

        if isinstance(value, QuerySet):
            # The values it selects, else its primary key - or the rows it combines, whose
            # primary key is selected from them built into the filter.
            value = value._get_filter_value_compiler()
        if isinstance(value, AwaitableQuery):
            filter_value_query = value._get_filter_value_query()
            return PlanDescription.combine(
                ("query", QueryConnection.get_pinned_connection_name(filter_value_query)),
                (filter_value_query.get_plan_description(context),),
            )
        return value.get_plan_description(context)

    @staticmethod
    def is_constant_list_condition(key: str, value: Any) -> bool:
        """Whether an ``__in``/``__not_in`` filter resolves to a constant condition - its list
        holds no value but None (``1=0``, ``IS NULL``, and their negations).

        Args:
            key: The filter key.
            value: The filter value.

        Returns:
            True for such a filter.
        """
        return (
            key.endswith(LIST_LOOKUP_SUFFIXES)
            and isinstance(value, (list, tuple, set))
            and all(item is None for item in value)
        )

    @staticmethod
    def is_union_query(value: Any) -> bool:
        """Whether a filter value is a set operation of querysets (``a.union(b)``).

        Args:
            value: The value.

        Returns:
            True for a queryset combining querysets of model instances.
        """
        # Deferred import: hare.query.queryset imports this module at import time.
        from hare.query.queryset import QuerySet
        from hare.query.queryset.combination.queryset_combination import QuerySetCombination

        return (
            isinstance(value, QuerySet)
            and value._combination is not None
            and not QuerySetCombination.selects_values(value)
        )
