from __future__ import annotations

import functools
import types
import typing
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Union

from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class VariantField(Field[Any]):
    """``Variant(T, ...)`` - a value of one of several fields' types, each value kept with the type of
    the field taking it::

        reading = VariantField([fields.IntField(), fields.CharField(max_length=20)], null=True)

    A value is taken by the first field whose type of value it is exactly, else by the first it is an
    instance of (an ``IntEnum`` by an int field), else by the first converting a value of its type (a
    text by an address field); a value read is given back by the field whose
    ClickHouse type the drivers read as its Python type - so the fields are read as distinct Python
    types (an ``IntField`` and a ``DecimalField``, not two text fields). A filter matches held values of
    the filter value's field only, and a path ``<n>`` reads the held values of the field at that
    position, NULL (a container's empty value) for the others (``reading__0__gt=3``). ClickHouse orders
    no rows by a ``Variant`` column. The column holds NULL itself - its fields take no ``null=True``.

    Args:
        variant_fields: The fields of the values - at least one, each an instance.

    Raises:
        ConfigurationError: No fields are given, or one isn't a field instance.
    """

    field_type = object
    SUPPORTED_DIALECTS = frozenset({ClickhouseDialectName.CLICKHOUSE})
    keeps_native_db_values = False

    def __init__(self, variant_fields: Sequence[Field[Any]], **kwargs: Any) -> None:
        # Checked as given - a caller may pass anything.
        given_fields: object = variant_fields
        if (
            isinstance(given_fields, str | Field)
            or not isinstance(given_fields, Sequence)
            or not given_fields
            or any(not isinstance(variant_field, Field) for variant_field in given_fields)
        ):
            raise ConfigurationError(
                f"VariantField(variant_fields=...) takes a list of one or more field instances, got {variant_fields!r}"
            )
        self.variant_fields = list(variant_fields)
        super().__init__(**kwargs)

    @functools.cached_property
    def server_types(self) -> list[str]:
        """The ClickHouse type of each field's values, named as the server names it."""
        # Local import: the ClickHouse types read the dialect's constants, which build this module's dialect.
        from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
        from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames

        return [
            ClickhouseTypeNames.get_server_type(variant_field.get_column_type(CLICKHOUSE_DIALECT))
            for variant_field in self.variant_fields
        ]

    @functools.cached_property
    def read_python_types(self) -> list[tuple[type, ...]]:
        """The Python types the drivers read each field's values as."""
        # Local import: the ClickHouse types read the dialect's constants, which build this module's dialect.
        from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames

        return [ClickhouseTypeNames.get_read_python_types(server_type) for server_type in self.server_types]

    @functools.cached_property
    def value_types(self) -> list[tuple[type, ...]]:
        """The Python types of each field's values - a container's is its own (a list, a dict)."""
        value_types = []
        for variant_field in self.variant_fields:
            annotation = variant_field.get_value_annotation()
            members = (
                typing.get_args(annotation)
                if typing.get_origin(annotation) in (Union, types.UnionType)
                else (annotation,)
            )
            value_types.append(
                tuple(
                    member_type
                    for member_type in (typing.get_origin(member) or member for member in members)
                    if isinstance(member_type, type)
                )
            )
        return value_types

    @functools.cached_property
    def converted_value_types(self) -> list[tuple[type, ...]]:
        """The Python type each field converts a value from, where it is one - a text an address field
        reads."""
        return [
            (variant_field.field_type,) if isinstance(variant_field.field_type, type) else ()
            for variant_field in self.variant_fields
        ]

    def get_variant_index(self, value: Any) -> int:
        """The position of the field taking a value.

        Args:
            value: The value - not None.

        Returns:
            The position.

        Raises:
            ValidationError: No field takes a value of its type.
        """
        value_type = type(value)
        for value_types in (self.value_types, self.converted_value_types):
            for index, field_value_types in enumerate(value_types):
                if value_type in field_value_types:
                    return index
            for index, field_value_types in enumerate(value_types):
                if isinstance(value, field_value_types):
                    return index
        field_names = ", ".join(type(variant_field).__name__ for variant_field in self.variant_fields)
        raise ValidationError(
            f"{self.model_field_name}: none of {field_names} takes a value of type {value_type.__name__}"
        )

    def to_python(self, value: Any) -> Any:
        if value is None:
            return None
        return self.variant_fields[self.get_variant_index(value)].to_python(value)

    def get_python_type(self) -> Any:
        python_types = tuple(
            dict.fromkeys(variant_field.get_value_annotation() for variant_field in self.variant_fields)
        )
        return python_types[0] if len(python_types) == 1 else Union[python_types]

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the lookups build hare's SQL terms, whose modules import the fields.
        from hare.dialects.clickhouse.lookups.clickhouse_typed_value_lookups import ClickhouseTypedValueLookups
        from hare.dialects.clickhouse.lookups.constants import (
            CLICKHOUSE_VARIANT_ELEMENT_FUNCTION_NAME,
            CLICKHOUSE_VARIANT_TYPE_FUNCTION_NAME,
        )

        return ClickhouseTypedValueLookups(
            CLICKHOUSE_VARIANT_TYPE_FUNCTION_NAME, CLICKHOUSE_VARIANT_ELEMENT_FUNCTION_NAME
        ).get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        if not segment.isdigit() or int(segment) >= len(self.variant_fields):
            return super().get_path_transform(segment)
        # Local import: hare's SQL terms import the fields package.
        from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_VARIANT_ELEMENT_FUNCTION_NAME
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        index = int(segment)
        held_type = self.server_types[index]

        def get_held_values(term: Term) -> Term:
            return Function(
                CLICKHOUSE_VARIANT_ELEMENT_FUNCTION_NAME, term, ValueWrapper(held_type, allow_parametrize=False)
            )

        return get_held_values, self.variant_fields[index]
