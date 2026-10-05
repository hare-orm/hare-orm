from __future__ import annotations

from collections.abc import Iterable
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.query.queryset.arguments.values_arguments import ValuesArguments
from hare.query.queryset.combination.queryset_combination import QuerySetCombination

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet


class ObjectsByKey:
    """``QuerySet.in_bulk()``: the objects of a queryset by the value of a unique field."""

    @staticmethod
    async def get(queryset: QuerySet[Any, Any], id_list: Iterable[Any] | None, field_name: str) -> dict[Any, Any]:
        """The objects of the queryset - only the ones holding a value of ``id_list`` when given - by
        their value of the field.

        Args:
            queryset: The queryset.
            id_list: The values looked for, None for every object.
            field_name: The field - ``"pk"``, a unique field, or the one field of ``distinct()``.

        Returns:
            The objects by value - a tuple of a composite primary key's values.

        Raises:
            QueryError: The queryset selects values, combines querysets or is sliced, or the field isn't
                unique.
            FieldError: The model has no such field.
        """
        ValuesArguments.raise_if_values_selected(queryset, "in_bulk")
        QuerySetCombination.raise_if_combined(queryset, "in_bulk")
        if queryset._limit is not None or queryset._offset:
            raise QueryError("in_bulk() can't be used on a sliced queryset (after .limit()/.offset()).")
        read_key = attrgetter(*ObjectsByKey.get_key_attribute_names(queryset, field_name))
        if id_list is not None:
            values = list(id_list)
            if not values:
                return {}
            queryset = queryset.filter(**{f"{field_name}__in": values})
        return {read_key(obj): obj for obj in await queryset}

    @staticmethod
    def get_key_attribute_names(queryset: QuerySet[Any, Any], field_name: str) -> tuple[str, ...]:
        """The attributes of an object holding its value of the field - a relation's key ones.

        Args:
            queryset: The queryset.
            field_name: The field.

        Returns:
            The attribute names.

        Raises:
            QueryError: The field isn't unique.
            FieldError: The model has no such field.
        """
        model = queryset.model
        meta = model._meta
        if field_name == "pk":
            return tuple(meta.primary_key_attribute_names)
        if field_name not in meta.fields_map:
            raise FieldError(f"in_bulk(): {model.__name__} has no field {field_name!r}")
        attribute_names = tuple(DeclaredUniqueness.get_attribute_names(model, (field_name,)))
        is_unique = any(
            uniqueness.attribute_names == attribute_names and uniqueness.condition is None
            for uniqueness in DeclaredUniqueness.get_declared(model)
        )
        if not is_unique and list(queryset._distinct_on) != [field_name]:
            raise QueryError(f"in_bulk()'s field_name must be a unique field, but {field_name!r} isn't.")
        return attribute_names
