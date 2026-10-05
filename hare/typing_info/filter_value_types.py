from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from hare.query.enums import Lookup, LookupTarget
from hare.typing_info.declarations import ValueType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.lookup_info.lookup_info import LookupInfo


class FilterValueTypes:
    """The types of the values a filter key takes and ``values()`` gives - read from the key's
    ``LookupInfo``, for the mypy plugin and ``hare stubs`` alike."""

    @staticmethod
    def get_filter_value_type(lookup_info: LookupInfo) -> ValueType:
        """The type of the value a filter key takes - a value, an iterable or a two-item range of the
        compared type, or an expression, a term or a subquery.

        Args:
            lookup_info: The description of the key.

        Returns:
            The type.
        """
        compared_type = FilterValueTypes.get_compared_type(lookup_info, accepts_instances=True)
        return dataclasses.replace(compared_type, shape=lookup_info.value_shape, accepts_expressions=True)

    @staticmethod
    def get_selected_value_type(lookup_info: LookupInfo) -> ValueType:
        """The type of the value ``values()``/``values_list()`` select for a path.

        Args:
            lookup_info: The description of the path, as a filter key.

        Returns:
            The type - None included when the value can be missing.
        """
        selected_type = FilterValueTypes.get_compared_type(lookup_info, accepts_instances=False)
        return dataclasses.replace(selected_type, nullable=FilterValueTypes.may_be_null(lookup_info))

    @staticmethod
    def get_compared_type(lookup_info: LookupInfo, *, accepts_instances: bool) -> ValueType:
        """The type of one value a filter key compares.

        Args:
            lookup_info: The description of the key.
            accepts_instances: Whether a relation takes an instance of its model besides its key - a
                filter does.

        Returns:
            The type.
        """
        target = lookup_info.target
        generic_field = lookup_info.generic_field
        if target is LookupTarget.GENERIC_RELATION and generic_field is not None:
            if lookup_info.compares_generic_type:
                return ValueType(literals=tuple(generic_field.branch_names))
            return ValueType(models=tuple(generic_field.branch_by_model))
        if target in {LookupTarget.JSON_PATH, LookupTarget.ANNOTATION}:
            return ValueType(is_any=True)
        compared_type = FilterValueTypes.get_compared_field_type(lookup_info, accepts_raw_values=accepts_instances)
        if target is LookupTarget.RELATION and accepts_instances and lookup_info.lookup is not Lookup.ISNULL:
            field = lookup_info.field
            key_field = field[0] if isinstance(field, tuple) else field
            if key_field is not None:
                compared_type = dataclasses.replace(compared_type, models=(key_field.model,))
        if lookup_info.lookup is Lookup.EXACT and accepts_instances and FilterValueTypes.may_be_null(lookup_info):
            compared_type = dataclasses.replace(compared_type, nullable=True)
        return compared_type

    @staticmethod
    def get_compared_field_type(lookup_info: LookupInfo, *, accepts_raw_values: bool) -> ValueType:
        """The type of the field value a key compares - the declared type of the field when the
        lookup compares the field's own value (a JSON field's only for equality: its other lookups
        take any part of a document), else the type the lookup takes.

        Args:
            lookup_info: The description of the key.
            accepts_raw_values: Whether an enum field takes its members' values too - a filter does.

        Returns:
            The type, without None.
        """
        field = lookup_info.field
        compares_field_value = (
            not lookup_info.transforms
            and lookup_info.target is not LookupTarget.RELATION
            and field is not None
            and not isinstance(field, tuple)
            and (
                lookup_info.value_type is field.field_type
                or (lookup_info.value_type is object and lookup_info.lookup is Lookup.EXACT)
            )
        )
        if compares_field_value and field is not None and not isinstance(field, tuple):
            return ValueType(
                classes=(lookup_info.value_type,),
                declared_in=(field.model, field.model_field_name),
                accepts_enum_values=accepts_raw_values,
            )
        return ValueType(classes=(lookup_info.value_type,))

    @staticmethod
    def may_be_null(lookup_info: LookupInfo) -> bool:
        """Whether a key's value may be NULL - its field is nullable, or it crosses a nullable or
        to-many relation.

        Args:
            lookup_info: The description of the key.

        Returns:
            Whether it may be NULL.
        """
        field = lookup_info.field
        fields = field if isinstance(field, tuple) else () if field is None else (field,)
        return (
            lookup_info.crosses_to_many
            or any(field.null for field in fields)
            or any(getattr(relation, "null", False) for relation in lookup_info.relations)
        )
