from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.contrib.request_query.constants import (
    DESCENDING_PREFIX,
    EMPTY_TEXT_MATCHING_LOOKUPS,
    MISSING_FILTER_ERROR_TYPE,
)
from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration
from hare.contrib.request_query.declaration.request_query_parameters import RequestQueryParameters
from hare.contrib.request_query.enums import EachRowOnce
from hare.contrib.request_query.exceptions import InvalidRequestQuery
from hare.contrib.request_query.value_target import ValueTarget
from hare.exceptions import QueryError
from hare.models import Model
from hare.query.expressions import Q
from hare.query.expressions.ordering import Ordering
from hare.query.queryset import QuerySet

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import OrderingItem, RequestQuery
    from hare.query.lookup_info.ordering_info import OrderingInfo


ModelType = TypeVar("ModelType", bound="Model")


class RequestQueryBuilding:
    """Combines request filters, access conditions, ordering and selected fields into querysets."""

    @staticmethod
    async def get_request_conditions(
        request_query: RequestQuery[ModelType], *, skipped_target: ValueTarget | None = None
    ) -> list[Q]:
        """The conditions of the request's parameters and search. The async ``filter_<parameter>``
        methods of the parameters with a value run concurrently; the conditions keep the order the
        parameters are declared in.

        Args:
            skipped_target: The values whose own filters are left out - ``count_by()`` counts a
                field under every filter of the request but its own.

        Returns:
            The parameters' conditions joined by ``Meta.join_type`` as one condition, and the
            search's as another; an empty list when the request filters by nothing.
        """
        declaration = request_query.get_declaration()
        # Each parameter's condition in declaration order: an async filter method's is awaited below,
        # every one of them at once.
        parameter_conditions: list[Any] = []
        for filter_declaration in declaration.filters:
            value = getattr(request_query, filter_declaration.parameter)
            if value is None or request_query.is_skipped(filter_declaration, skipped_target):
                continue
            if (
                value == ""
                and filter_declaration.lookup_info is not None
                and filter_declaration.lookup_info.lookup in EMPTY_TEXT_MATCHING_LOOKUPS
            ):
                # Matches every row - no filter, as a blank search is none; delete() and update()
                # need one.
                continue
            if filter_declaration.method_name is not None:
                method = getattr(request_query, filter_declaration.method_name)
                parameter_conditions.append(method(value))
                continue
            parameter_conditions.append(Q(**{filter_declaration.filter_key: value}))  # type: ignore[misc]
        pending_positions = [
            position for position, condition in enumerate(parameter_conditions) if inspect.isawaitable(condition)
        ]
        if pending_positions:
            awaited_conditions = await asyncio.gather(
                *(parameter_conditions[position] for position in pending_positions)
            )
            for position, condition in zip(pending_positions, awaited_conditions, strict=True):
                parameter_conditions[position] = condition
        filter_conditions: list[Q] = [condition for condition in parameter_conditions if condition is not None]
        conditions = []
        if filter_conditions:
            conditions.append(Q.with_connector(declaration.join_type, *filter_conditions))
        search_condition = request_query.get_search_condition()
        if search_condition is not None:
            conditions.append(search_condition)
        return conditions

    @staticmethod
    def get_search_condition(request_query: RequestQuery[ModelType]) -> Q | None:
        """The condition of the request's search.

        Returns:
            The condition, None without a search or a search text.
        """
        search = request_query.get_declaration().search
        return (
            None
            if search is None
            else search.get_condition(RequestQueryParameters.get_parameter_values(request_query))
        )

    @staticmethod
    def crosses_to_many(request_query: RequestQuery[ModelType], queryset: QuerySet[Any], condition: Q) -> bool:
        """Whether a condition joins a relation to many rows - the query can then return a row more
        than once.

        Args:
            queryset: The queryset the condition filters.
            condition: The condition.

        Returns:
            True when a filter key of the condition or of a condition inside it crosses such a
            relation.

        Raises:
            FieldError: A filter key names no field, relation or annotation.
        """
        declaration = request_query.get_declaration()
        return any(declaration.get_lookup_info(queryset, key).crosses_to_many for key in condition.filters) or any(
            request_query.crosses_to_many(queryset, child) for child in condition.children
        )

    @staticmethod
    async def build_queryset(
        request_query: RequestQuery[ModelType],
        method_name: str,
        *,
        each_row_once: EachRowOnce,
        skipped_target: ValueTarget | None = None,
        needs_filter: bool = False,
    ) -> QuerySet[ModelType]:
        """The queryset with the handler's changes and every condition: the request's parameters
        and search, the handler's ``where()``, the access condition, the latest versions.

        Args:
            method_name: The method asking, for the error.
            each_row_once: How to take each row once when a condition joins a relation to many
                rows.
            skipped_target: The values whose own filters are left out.
            needs_filter: Refuse a request nothing but the access condition filters - a delete or an
                update never changes every row the user may see.

        Returns:
            The queryset, not ordered.

        Raises:
            RequestQueryForbidden: See ``apply_deleted()``.
            QueryError: See ``get_rows_once()``.
            InvalidRequestQuery: ``needs_filter`` and neither the request's parameters and search nor
                the handler's ``where()`` give a condition - a filter method returning None gives
                none; the request's fault, answered as an invalid request.
        """
        queryset = await request_query.get_rows_queryset()
        conditions = [
            *await request_query.get_request_conditions(skipped_target=skipped_target),
            *request_query._conditions,
        ]
        if needs_filter and not any(conditions):
            raise InvalidRequestQuery(
                [
                    {
                        "loc": [],
                        "msg": (
                            f"{type(request_query).__qualname__}.{method_name}() needs a filter - "
                            "the request filters by nothing"
                        ),
                        "type": MISSING_FILTER_ERROR_TYPE,
                    }
                ]
            )
        access_condition = await request_query.get_access_condition()
        if access_condition is not None:
            conditions.append(access_condition)
        versions = request_query.get_declaration().versions
        if versions is not None:
            all_versions = await request_query.apply_deleted(request_query.get_queryset())
            conditions.append(versions.get_condition(all_versions, access_condition))
        if not conditions:
            return queryset
        queryset = queryset.filter(*conditions)
        if each_row_once is EachRowOnce.NOT_NEEDED or not any(
            request_query.crosses_to_many(queryset, condition) for condition in conditions
        ):
            return queryset
        if each_row_once is EachRowOnce.DISTINCT:
            return queryset.distinct()
        return await request_query.get_rows_once(queryset, method_name)

    @staticmethod
    def is_skipped(filter_declaration: FilterDeclaration, skipped_target: ValueTarget | None) -> bool:
        """Whether a filter is one of the left-out values' own.

        Args:
            filter_declaration: The filter.
            skipped_target: The left-out values, None for none.

        Returns:
            True when the filter compares those values - a filter method never is.
        """
        if skipped_target is None or filter_declaration.lookup_info is None:
            return False
        return ValueTarget.of(filter_declaration.lookup_info) == skipped_target

    @staticmethod
    def get_ordering(
        request_query: RequestQuery[ModelType], queryset: QuerySet[ModelType]
    ) -> tuple[OrderingItem, ...]:
        """The ordering of the rows: the handler's ``order_by()``, else the request's, else
        ``OrderingConfig.default``, else the queryset's own, else the model's ``Meta.ordering`` - and
        then the primary key's fields that aren't ordered by yet, so equal values keep one order. A
        model without a primary key gets no such fields.

        Args:
            queryset: The queryset being ordered.

        Returns:
            The ordering.
        """
        declaration = request_query.get_declaration()
        ordering = declaration.ordering
        items: tuple[OrderingItem, ...] = ()
        if request_query._explicit_ordering is not None:
            items = request_query._explicit_ordering
        elif ordering is not None and (
            requested := ordering.get_requested(RequestQueryParameters.get_parameter_values(request_query))
        ):
            items = requested
        elif ordering is not None and ordering.default:
            items = ordering.default
        elif queryset.orderings:
            items = tuple(Ordering(field_name, order) for field_name, order in queryset.orderings)
        elif queryset.model._meta.ordering:
            ordering_expressions = queryset.model._meta.ordering_expressions
            items = tuple(
                Ordering(ordering_expressions.get(field_name, field_name), order)
                for field_name, order in queryset.model._meta.ordering
            )
        if not declaration.key_ordering_paths:
            return items
        ordered_paths = {path for item in items for path in request_query.get_ordering_info(queryset, item).paths}
        return (*items, *(path for path in declaration.key_ordering_paths if path not in ordered_paths))

    @staticmethod
    def get_ordering_name(item: OrderingItem) -> str:
        """The name of an ordering item without its direction.

        Args:
            item: A name with ``-`` for descending, or an ordering expression.

        Returns:
            The name.
        """
        if not isinstance(item, Ordering):
            return item.removeprefix(DESCENDING_PREFIX)
        if not isinstance(item.target, str):
            raise QueryError(
                f"A request query orders by names - {item!r} orders by an expression; annotate it and order "
                "by the annotation's name"
            )
        return item.target

    @staticmethod
    def get_ordering_info(
        request_query: RequestQuery[ModelType], queryset: QuerySet[Any], item: OrderingItem
    ) -> OrderingInfo:
        """The ORM's description of one ordering item, from the class's declaration - read once
        per name, not on every request.

        Args:
            queryset: The queryset being ordered.
            item: The ordering item.

        Returns:
            The description: the names the query orders by (a relation's key column, ``pk``'s key
            fields), the fields and the relations crossed.
        """
        return request_query.get_declaration().get_ordering_info(queryset, request_query.get_ordering_name(item))

    @staticmethod
    def apply_selection(
        request_query: RequestQuery[ModelType], queryset: QuerySet[ModelType], ordering: Iterable[OrderingItem] = ()
    ) -> QuerySet[ModelType]:
        """Loads what the request's fields and include parameters ask for: only the named fields
        (with the primary key and the fields the ordering reads), and the named relations.

        Args:
            queryset: The queryset of the rows.
            ordering: The ordering of the rows - its own fields are loaded too.

        Returns:
            The queryset.
        """
        declaration = request_query.get_declaration()
        parameter_values = RequestQueryParameters.get_parameter_values(request_query)
        if declaration.fields is not None:
            ordered_fields: list[str] = []
            for item in ordering:
                ordering_info = request_query.get_ordering_info(queryset, item)
                if not ordering_info.relations:
                    ordered_fields.extend(ordering_info.paths)
            queryset = declaration.fields.apply(
                queryset,
                parameter_values,
                [*declaration.key_ordering_paths, *ordered_fields],
                declaration.api_target_fields,
            )
        if declaration.include is not None:
            queryset = declaration.include.apply(queryset, parameter_values, declaration.include_to_many)
        return queryset

    @staticmethod
    def get_ordered_queryset(
        request_query: RequestQuery[ModelType], queryset: QuerySet[ModelType]
    ) -> QuerySet[ModelType]:
        """A queryset ordered by ``get_ordering()``, loading what the request's fields and include
        parameters ask for.

        Args:
            queryset: The filtered queryset.

        Returns:
            The queryset.
        """
        ordering = request_query.get_ordering(queryset)
        return request_query.apply_selection(queryset.order_by(*ordering), ordering)
