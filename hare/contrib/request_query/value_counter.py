"""How many of the rows a request asks for have each value of a field - for the filters of a list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.contrib.request_query.constants import DESCENDING_PREFIX, ROW_COUNT_ANNOTATION, ROW_COUNT_SQL
from hare.contrib.request_query.enums import EachRowOnce
from hare.contrib.request_query.value_target import ValueTarget
from hare.exceptions import FieldError, QueryError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions.raw_sql import RawSQL
from hare.query.queryset.selection.statement_selection import StatementSelection

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.query.queryset import QuerySet


class ValueCounter:
    """Counts the rows of a request query by the values of its fields.

    Each field is counted under every filter of the request except its own (``?status=draft``
    still counts every status), with the search, the handler's ``where()`` and the access
    condition. A relation counts by the related key - a tuple for a composite key; a relation to
    many rows counts each row once per related row; ``None`` counts the rows without a value.

    Args:
        request_query: The request query.
    """

    def __init__(self, request_query: RequestQuery[Any]) -> None:
        self.request_query = request_query

    async def count(self, names: tuple[str, ...], limit: int | None) -> dict[str, dict[Any, int]]:
        """The counts of each field.

        Args:
            names: The fields - fields or relations, through relations (``status``, ``author``,
                ``tags``, ``author__profile__city``).
            limit: How many of the most frequent values of each field to return, None for all.

        Returns:
            For each field, the count of rows by value, most frequent first.

        Raises:
            FieldError: A name isn't a field or relation of the model.
            QueryError: ``limit`` isn't a positive int, or a filter crosses a relation to many rows
                on a model without a primary key.
        """
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise QueryError(
                f"count_by() limit must be a positive int or None, got {ValueDescriptions.get_shown_value(limit)}"
            )
        describing_queryset = self.request_query.get_queryset()
        return {name: await self.count_values(describing_queryset, name, limit) for name in names}

    async def count_values(self, describing_queryset: QuerySet[Any], name: str, limit: int | None) -> dict[Any, int]:
        """The counts of one field.

        Args:
            describing_queryset: The query's rows, which describe the field.
            name: The field.
            limit: How many of the most frequent values to return, None for all.

        Returns:
            The count of rows by value, most frequent first.
        """
        request_query = self.request_query
        ordering_info = request_query.get_ordering_info(describing_queryset, name)
        # Rows grouped by an encrypted field's random ciphertext count one row a group.
        method_name = f"{type(request_query).__qualname__}.count_by()"
        for field_object in ordering_info.fields:
            EncryptedFieldBase.raise_if_encrypted(field_object, method_name)
            # The counts are keyed by the values - a sensitive field's would be handed out.
            if field_object is not None and field_object.sensitive:
                raise FieldError(
                    f"{method_name} can't count by {name!r}: {field_object.model_field_name} is sensitive, and the "
                    "counts are keyed by its values"
                )
        target = ValueTarget.of(describing_queryset.get_lookup_info(name))
        paths = ordering_info.paths
        queryset = await request_query.build_queryset(
            "count_by", each_row_once=EachRowOnce.BY_KEY, skipped_target=target
        )
        # The counts are values - a select_related() or prefetch of the declared queryset has nothing
        # to attach to.
        grouped = (
            StatementSelection.get_values_rows_queryset(queryset)
            .annotate(**{ROW_COUNT_ANNOTATION: RawSQL(ROW_COUNT_SQL)})
            .group_by(*paths)
            .order_by(f"{DESCENDING_PREFIX}{ROW_COUNT_ANNOTATION}", *paths)
        )
        if limit is not None:
            grouped = grouped.limit(limit)
        rows = await grouped.values(*paths, ROW_COUNT_ANNOTATION)
        return {ValueCounter.get_value(row, paths): row[ROW_COUNT_ANNOTATION] for row in rows}

    @staticmethod
    def get_value(row: dict[str, Any], paths: tuple[str, ...] | list[str]) -> Any:
        """The value a row of counts counts - a tuple for a composite key, None for a row without
        one.

        Args:
            row: The row.
            paths: The paths of the value's columns.

        Returns:
            The value.
        """
        if len(paths) == 1:
            return row[paths[0]]
        values = tuple(row[path] for path in paths)
        return None if all(value is None for value in values) else values
