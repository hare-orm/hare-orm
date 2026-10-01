"""The parameters ``Meta.filters`` declares and the descriptions of filter parameters, built from the
ORM's descriptions of their filters once the models are bound."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo

from hare.contrib.request_query.constants import ENUM_VALUE_LOOKUPS
from hare.contrib.request_query.declaration.declaration_builder import DeclarationBuilder
from hare.contrib.request_query.options.filter import Filter
from hare.contrib.request_query.options.filter_field import FilterField
from hare.contrib.request_query.types.key_columns import KeyColumns
from hare.contrib.request_query.value_annotations import ValueAnnotation
from hare.exceptions import ConfigurationError
from hare.fields.base.field import Field
from hare.query.enums import LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.base import RequestQuery
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.queryset import QuerySet


class FilterParameterBuilder:
    """Builds the parameters of one request query class from its ``Meta.filters``, and describes
    its filter parameters for the API schema.

    ``Meta.filters`` is a tuple of ``FilterField`` - a field, a path through relations too, and
    the lookups a request may filter it by: ``FilterField("status", lookups=(Lookup.EXACT,
    Lookup.IN))`` makes the parameters ``status`` and ``status__in``, each typed by the value its
    filter takes; one named otherwise (``parameter="city"``) filters by its path all the same.
    """

    def __init__(self, request_query_class: type[RequestQuery[Any]]) -> None:
        self.request_query_class = request_query_class
        self.class_name = request_query_class.__qualname__
        self.declaration_builder = DeclarationBuilder(request_query_class)

    @property
    def described_models(self) -> set[Any]:
        """Every model the descriptions ``build_fields()`` read read - the parameters are stale
        once one of them changes."""
        return self.declaration_builder.described_models

    def build_fields(self, queryset: QuerySet[Any]) -> dict[str, FieldInfo]:
        """The pydantic fields to add or replace: the parameters of ``Meta.filters``, and each
        filter parameter the class declares without a description, with one.

        Args:
            queryset: The class's queryset.

        Returns:
            The fields by parameter.

        Raises:
            ConfigurationError: ``Meta.filters`` isn't a tuple of ``FilterField``, names a filter
                the model doesn't have or one taking a value a parameter can't carry, or makes a
                parameter the class declares itself or an option adds.
        """
        declared_fields = self.request_query_class.model_fields
        option_parameters = {
            parameter
            for option in self.request_query_class.get_request_options()
            for parameter in option.get_parameters()
        }
        declaration_builder = self.declaration_builder
        fields: dict[str, FieldInfo] = {}
        for parameter, (filter_key, filter_field) in self.get_filter_fields().items():
            if parameter in declared_fields or parameter in option_parameters:
                raise ConfigurationError(
                    f"{self.class_name}.Meta.filters makes the parameter {parameter!r}, which the class already has - "
                    "declare it in one place"
                )
            lookup_info = declaration_builder.describe_filter(queryset, f"{self.class_name}.Meta.filters", filter_key)
            annotation = self.get_annotation(lookup_info)
            if parameter != filter_key:
                annotation = Annotated[annotation, Filter(filter_key)]
            description = filter_field.description or self.get_description(lookup_info)
            fields[parameter] = FieldInfo.from_annotated_attribute(
                annotation, PydanticField(default=None, description=description)
            )
        for parameter, field_info in declared_fields.items():
            if parameter in option_parameters or field_info.description is not None:
                continue
            parameter_filter = declaration_builder.get_parameter_filter(parameter, field_info)
            if parameter_filter is None or parameter_filter.filter_key is None:
                continue
            lookup_info = declaration_builder.describe_filter(
                queryset, f"{self.class_name}.{parameter}", parameter_filter.filter_key
            )
            description = self.get_description(lookup_info)
            if description is not None:
                fields[parameter] = FieldInfo.merge_field_infos(field_info, description=description)
        return fields

    def get_filter_fields(self) -> dict[str, tuple[str, FilterField]]:
        """The parameters ``Meta.filters`` declares.

        Returns:
            Each parameter's ``.filter()`` key and the ``FilterField`` declaring it, by parameter.

        Raises:
            ConfigurationError: ``Meta.filters`` isn't a tuple of ``FilterField``, or two of them make
                the same parameter.
        """
        filters = self.request_query_class.get_meta_option("filters")
        if filters is None:
            return {}
        if not isinstance(filters, tuple) or not all(isinstance(item, FilterField) for item in filters):
            raise ConfigurationError(f"{self.class_name}.Meta.filters must be a tuple of FilterField, got {filters!r}")
        filter_fields: dict[str, tuple[str, FilterField]] = {}
        for filter_field in filters:
            for parameter, filter_key in filter_field.get_filter_keys().items():
                if parameter in filter_fields:
                    raise ConfigurationError(f"{self.class_name}.Meta.filters makes the parameter {parameter!r} twice")
                filter_fields[parameter] = (filter_key, filter_field)
        return filter_fields

    @classmethod
    def get_annotation(cls, lookup_info: LookupInfo) -> Any:
        """The annotation of a parameter taking a filter's value.

        Args:
            lookup_info: The ORM's description of the filter.

        Returns:
            ``T | None`` for one value, ``list[T] | None`` for a list, ``tuple[T, T] | None`` for a
            range - ``T`` the value's type: the field's enum where the lookup compares the field's
            own values, ``KeyColumns[...]`` for a composite key.

        Raises:
            ConfigurationError: The filter takes a value of a type only known when the query runs
                (an annotation) or any JSON value - such a parameter is declared by hand.
        """
        item_type = cls.get_item_type(lookup_info)
        if lookup_info.value_shape is LookupValueShape.LIST:
            return list[item_type] | None  # type: ignore[valid-type]
        if lookup_info.value_shape is LookupValueShape.RANGE:
            return tuple[item_type, item_type] | None  # type: ignore[valid-type]
        return item_type | None

    @staticmethod
    def get_item_type(lookup_info: LookupInfo) -> Any:
        """The type of one value a filter takes - of each item of a list or range.

        Args:
            lookup_info: The ORM's description of the filter.

        Returns:
            The type.

        Raises:
            ConfigurationError: See ``get_annotation()``.
        """
        value_type = lookup_info.value_type
        if isinstance(value_type, tuple):
            return KeyColumns.__class_getitem__(value_type)
        if value_type is None or value_type is object or not isinstance(value_type, type):
            raise ConfigurationError(
                f"The filter {lookup_info.key!r} takes {ValueAnnotation.describe_type(value_type)} - declare its "
                "parameter with the type it takes instead of listing it in Meta.filters"
            )
        field = lookup_info.field
        if isinstance(field, Field) and not lookup_info.transforms and lookup_info.lookup in ENUM_VALUE_LOOKUPS:
            value_annotation = field.get_value_annotation()
            if isinstance(value_annotation, type) and issubclass(value_annotation, Enum):
                return value_annotation
        return value_type

    @staticmethod
    def get_description(lookup_info: LookupInfo) -> str | None:
        """The description of a filter parameter for the API schema: the field's own description,
        and how a list or a range is written.

        Args:
            lookup_info: The ORM's description of the filter.

        Returns:
            The description, None when there is nothing to say beyond the parameter's name and
            type.
        """
        parts: list[str] = []
        field = lookup_info.field
        if isinstance(field, Field) and field.description:
            parts.append(field.description)
        if lookup_info.value_shape is LookupValueShape.LIST:
            parts.append("Repeat the parameter for each value.")
        elif lookup_info.value_shape is LookupValueShape.RANGE:
            parts.append("Two values, from and to: repeat the parameter.")
        return " ".join(parts) or None
