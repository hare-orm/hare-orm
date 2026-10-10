from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.enums import CursorDirection, ParameterType
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.paginations.constants import DEFAULT_CURSOR_PARAMETER
from hare.contrib.request_query.paginations.cursor import Cursor
from hare.contrib.request_query.results.cursor_page import CursorPage
from hare.exceptions import ConfigurationError
from hare.query.expressions.ordering import Ordering

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import OrderingItem, RequestQuery
    from hare.models import Model
    from hare.query.lookup_info.ordering_info import OrderingInfo
    from hare.query.queryset import QuerySet
from hare.contrib.request_query.paginations.pagination import Pagination


@dataclasses.dataclass(frozen=True, slots=True)
class CursorPagination(Pagination):
    """Pages by a cursor - the ordering values of the row a page starts after or ends before -
    instead of an offset: a page costs the same wherever it is, and rows added or removed between
    requests don't shift the pages. The page is a ``CursorPage``; there is no total count.

    The ordering must read only the model's own fields and forward relations: no annotation, no
    relation to many rows; the model needs a primary key to order equal rows by.

    Args:
        cursor_parameter: The name of the cursor parameter.
    """

    cursor_parameter: str = DEFAULT_CURSOR_PARAMETER

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        return (
            *Pagination.get_parameter_fields(self),
            ParameterField(self.cursor_parameter, str | None, None, ParameterType.CURSOR),
        )

    def check_model(self, owner: str, model: type[Model]) -> None:
        if not model._meta.has_primary_key:
            raise ConfigurationError(
                f"{owner}: a cursor pagination needs a primary key to order equal rows by - {model.__name__} has none"
            )

    def check_ordering(self, owner: str, name: str, queryset: QuerySet[Any]) -> None:
        first_part = name.split("__", 1)[0]
        if first_part != "pk" and first_part not in queryset.model._meta.fields_map:
            raise ConfigurationError(
                f"{owner}.Meta.ordering: a cursor pagination can't order by the annotation {name!r}"
            )

    async def get_page(self, request_query: RequestQuery[Any]) -> CursorPage[Any]:
        limit: int = getattr(request_query, self.limit_parameter)
        cursor_text: str | None = getattr(request_query, self.cursor_parameter)
        queryset = await request_query.get_filtered_queryset()
        ordering = request_query.get_ordering(queryset)
        ordering_names = tuple(self.describe_ordering_item(item) for item in ordering)
        queryset = request_query.get_ordered_queryset(queryset)
        related_paths = self.get_ordering_relation_paths(request_query, queryset, ordering)
        if related_paths:
            queryset = queryset.select_related(*related_paths)
        cursor = (
            self.read_cursor(request_query, cursor_text, queryset, ordering, ordering_names) if cursor_text else None
        )
        backwards = cursor is not None and cursor.direction is CursorDirection.PREVIOUS
        if cursor is not None:
            queryset = queryset.before_cursor(*cursor.values) if backwards else queryset.after_cursor(*cursor.values)
        rows = list(await queryset.limit(limit + 1))
        has_more = len(rows) > limit
        if backwards:
            items = rows[-limit:] if has_more else rows
            has_next, has_previous = True, has_more
        else:
            items = rows[:limit]
            has_next, has_previous = has_more, cursor is not None
        next_cursor = (
            Cursor(CursorDirection.NEXT, ordering_names, queryset.cursor_values(items[-1])).encode()
            if has_next and items
            else None
        )
        previous_cursor = (
            Cursor(CursorDirection.PREVIOUS, ordering_names, queryset.cursor_values(items[0])).encode()
            if has_previous and items
            else None
        )
        items = await request_query.after_fetch(items)
        return CursorPage(
            result=items,
            limit=limit,
            next_cursor=next_cursor,
            previous_cursor=previous_cursor,
            next=self.get_url_with(request_query, {self.cursor_parameter: next_cursor}) if next_cursor else None,
            previous=self.get_url_with(request_query, {self.cursor_parameter: previous_cursor})
            if previous_cursor
            else None,
        )

    @staticmethod
    def describe_ordering_item(item: OrderingItem) -> str:
        """An ordering item as text, for a cursor to name the ordering it belongs to.

        Args:
            item: A name with ``-`` for descending, or an ordering expression.

        Returns:
            The name, or the expression's field and direction.
        """
        return f"{item.field_name} {item.order.value}" if isinstance(item, Ordering) else item

    @staticmethod
    def get_ordering_relation_paths(
        request_query: RequestQuery[Any], queryset: QuerySet[Any], ordering: Iterable[OrderingItem]
    ) -> list[str]:
        """The relations an ordering reads through - a cursor reads their fields off each row, so
        they are loaded with it.

        Args:
            request_query: The request query.
            queryset: The ordered queryset.
            ordering: The ordering.

        Returns:
            Each relation path once.
        """
        paths: dict[str, None] = {}
        for item in ordering:
            ordering_info: OrderingInfo = request_query.get_ordering_info(queryset, item)
            if ordering_info.relations and ordering_info.fields and ordering_info.fields[0] is not None:
                name = request_query.get_ordering_name(item)
                relation_path = "__".join(name.split("__")[: len(ordering_info.relations)])
                if relation_path != name:
                    paths[relation_path] = None
        return list(paths)

    def read_cursor(
        self,
        request_query: RequestQuery[Any],
        cursor_text: str,
        queryset: QuerySet[Any],
        ordering: tuple[OrderingItem, ...],
        ordering_names: tuple[str, ...],
    ) -> Cursor:
        """Reads a request's cursor against the query's ordering.

        Args:
            request_query: The request query.
            cursor_text: The request's cursor.
            queryset: The ordered queryset.
            ordering: The ordering.
            ordering_names: The ordering as text.

        Returns:
            The cursor.

        Raises:
            InvalidRequestQuery: The text isn't a cursor of this query and ordering.
        """
        value_types = tuple(
            None if field is None else field.field_type
            for item in ordering
            for field in request_query.get_ordering_info(queryset, item).fields
        )
        try:
            cursor = Cursor.decode(cursor_text, value_types)
        except ValueError as error:
            raise self.refuse(self.cursor_parameter, str(error), "cursor") from error
        if cursor.ordering != ordering_names:
            raise self.refuse(self.cursor_parameter, "The cursor belongs to another ordering", "cursor")
        return cursor
