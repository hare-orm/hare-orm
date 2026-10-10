from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.exceptions import FieldError
from hare.query.expressions import Expression
from hare.query.queryset.constants import DATES_ORDERS, DATES_VALUE_ALIAS

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.queryset.queryset import QuerySet


SameQuerySet = TypeVar("SameQuerySet", bound="QuerySet[Any, Any]")


class DateLists:
    """dates() and datetimes() - the distinct dates or datetimes of a field, truncated to a type."""

    @staticmethod
    def get_dates_field(
        queryset: QuerySet[Any, Any],
        field_name: str,
        method: str,
        field_classes: tuple[type[Field[Any]], ...],
        trunc_types: tuple[str, ...],
        trunc_type: str,
        order: str,
    ) -> Field[Any]:
        """The field ``dates()``/``datetimes()`` reads, its arguments checked.

        Args:
            queryset: The queryset.
            field_name: The field the dates are read off.
            method: ``dates`` or ``datetimes``, for the error messages.
            field_classes: The field classes the method takes.
            trunc_types: The truncations the method takes.
            trunc_type: The truncation asked for.
            order: ``"ASC"`` or ``"DESC"``.

        Raises:
            FieldError: See ``dates()``.
        """
        field = queryset.model._meta.fields_map.get(field_name)
        if not isinstance(field, field_classes):
            expected = " or ".join(field_class.__name__ for field_class in field_classes)
            raise FieldError(f"{method}() takes a {expected} of {queryset.model.__name__}, got {field_name!r}")
        if trunc_type not in trunc_types:
            raise FieldError(f"{method}() trunc_type must be one of {', '.join(trunc_types)}, got {trunc_type!r}")
        if order not in DATES_ORDERS:
            raise FieldError(f"{method}() order must be 'ASC' or 'DESC', got {order!r}")
        return field

    @staticmethod
    def get_distinct_dates(queryset: SameQuerySet, truncated: Expression, order: str) -> SameQuerySet:
        """The distinct, non-NULL values of a truncated date, in ``order``.

        Args:
            queryset: The queryset.
            truncated: The truncated value.
            order: ``ASC`` or ``DESC``.

        Returns:
            A ``values_list(flat=True)`` queryset.
        """
        ordering = DATES_VALUE_ALIAS if order == "ASC" else f"-{DATES_VALUE_ALIAS}"
        return cast(
            "SameQuerySet",
            queryset.alias(**{DATES_VALUE_ALIAS: truncated})
            .filter(**{f"{DATES_VALUE_ALIAS}__isnull": False})
            .order_by(ordering)
            .values_list(DATES_VALUE_ALIAS, flat=True)
            .distinct(),
        )
