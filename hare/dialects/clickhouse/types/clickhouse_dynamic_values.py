from __future__ import annotations

import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_DECIMAL_MAX_PRECISION,
    CLICKHOUSE_DYNAMIC_DATETIME_TYPE,
    CLICKHOUSE_DYNAMIC_INTEGER_TYPES,
    CLICKHOUSE_DYNAMIC_PLAIN_TYPES,
    CLICKHOUSE_DYNAMIC_TYPE,
)
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ValidationError
from hare.fields.data.containers.declarations import MapValue, TupleValue

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.fields.dynamic_field import DynamicField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseDynamicValues:
    """How ClickHouse writes and reads a ``Dynamic`` column - each value with the type its Python value
    has: ``Bool``, the first of ``Int64``/``UInt64``/``Int128``/``UInt128``/``Int256``/``UInt256``
    holding an int, ``Float64``, ``String``, ``Decimal(P, S)`` of a decimal's digits,
    ``DateTime64(6, 'UTC')``, ``Date32``, ``UUID``, ``IPv4``/``IPv6``, and ``Array`` (a list),
    ``Tuple`` (a tuple), ``Map`` (a dict) of the types of the values they hold - a list's (a dict's)
    values of one type, ``Nullable`` where one is None."""

    @staticmethod
    def get_column_type(field: Field[Any]) -> str:
        """``Dynamic``, or ``Dynamic(max_types=N)`` of a field limiting its types.

        Args:
            field: The dynamic field.

        Returns:
            The column type.
        """
        max_types = cast("DynamicField", field).max_types
        return CLICKHOUSE_DYNAMIC_TYPE if max_types is None else f"{CLICKHOUSE_DYNAMIC_TYPE}(max_types={max_types})"

    @classmethod
    def to_db(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value bound with its type.

        Args:
            field: The dynamic field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The typed value, written into the column's type.

        Raises:
            ValidationError: The value is None on a field holding no NULL, or has no ClickHouse type.
        """
        held_type = cls.get_column_type(field)
        if value is None:
            field.validate(value)
            return ClickhouseTypedValue(None, "Nothing", held_type)
        written_value, column_type = cls.get_written_value(value, field.model_field_name or "value")
        return ClickhouseTypedValue(written_value, column_type, held_type)

    @classmethod
    def to_lookup(cls, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value a filter compares, bound with its type.

        Args:
            field: The dynamic field.
            value: The Python value.
            instance: The model (class) the filter is built for.

        Returns:
            The typed value; None for None.

        Raises:
            ValidationError: The value has no ClickHouse type.
        """
        if value is None:
            return None
        written_value, column_type = cls.get_written_value(value, field.model_field_name or "value")
        return ClickhouseTypedValue(written_value, column_type)

    @classmethod
    def get_written_value(cls, value: Any, path: str) -> tuple[Any, str]:
        """A value as it is written, and its type.

        Args:
            value: The value - not None.
            path: Where the value is, named in an error.

        Returns:
            The value and its type.

        Raises:
            ValidationError: The value, or one it holds, has no ClickHouse type.
        """
        # A value of exactly one of the plain classes - nothing else to tell.
        plain_type = CLICKHOUSE_DYNAMIC_PLAIN_TYPES.get(value.__class__)
        if plain_type is not None:
            return value, plain_type
        while isinstance(value, Enum):
            value = value.value
        if isinstance(value, datetime.datetime):
            moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            return moment, CLICKHOUSE_DYNAMIC_DATETIME_TYPE
        # A boolean before the integers: it is one.
        for plain_class, plain_type in CLICKHOUSE_DYNAMIC_PLAIN_TYPES.items():
            if isinstance(value, plain_class):
                return value, plain_type
        if isinstance(value, int):
            return value, cls.get_integer_type([value], path)
        if isinstance(value, Decimal):
            return value, cls.get_decimal_type([value], path)
        if isinstance(value, list):
            elements, element_type = cls.get_held_values(value, f"{path}[]")
            return elements, f"Array({element_type})"
        if isinstance(value, tuple):
            return cls.get_written_tuple(value, path)
        if isinstance(value, dict):
            return cls.get_written_map(value, path)
        raise ValidationError(f"{path}: a value of type {type(value).__name__} has no ClickHouse type")

    @classmethod
    def get_written_tuple(cls, value: tuple[Any, ...], path: str) -> tuple[TupleValue, str]:
        """A tuple as it is written, and its type - each element of its own type.

        Args:
            value: The tuple.
            path: Where the value is, named in an error.

        Returns:
            The value and its type.

        Raises:
            ValidationError: An element has no ClickHouse type.
        """
        written_elements = []
        element_types = []
        for index, element in enumerate(value):
            (written_element,), element_type = cls.get_held_values([element], f"{path}[{index}]")
            written_elements.append(written_element)
            element_types.append(element_type)
        return TupleValue(written_elements), f"Tuple({', '.join(element_types)})"

    @classmethod
    def get_written_map(cls, value: dict[Any, Any], path: str) -> tuple[MapValue, str]:
        """A dict as it is written, and its type - its keys of one type, its values of one type.

        Args:
            value: The dict.
            path: Where the value is, named in an error.

        Returns:
            The value and its type.

        Raises:
            ValidationError: A key or a value has no ClickHouse type, or the keys are containers or
                None.
        """
        keys, key_type = cls.get_held_values(list(value), f"{path} key")
        if key_type.startswith(("Nullable(", "Array(", "Tuple(", "Map(")):
            raise ValidationError(f"{path}: a dict's keys are values of one type that isn't a container or None")
        items, item_type = cls.get_held_values(list(value.values()), f"{path}[]")
        return MapValue(zip(keys, items, strict=True)), f"Map({key_type}, {item_type})"

    @classmethod
    def get_held_values(cls, values: list[Any], path: str) -> tuple[list[Any], str]:
        """The values a container holds as they are written, and the one type they are of - the widest
        integer (decimal) type of ints (decimals), ``Nullable`` when one is None.

        Args:
            values: The values.
            path: Where the values are, named in an error.

        Returns:
            The values and their type - ``Nothing`` (``Nullable(Nothing)``) for none (only None).

        Raises:
            ValidationError: The values are of several types, or a None is among containers.
        """
        unwrapped_values = []
        for value in values:
            while isinstance(value, Enum):
                value = value.value
            unwrapped_values.append(value)
        present_values = [value for value in unwrapped_values if value is not None]
        has_none = len(present_values) < len(unwrapped_values)
        if not present_values:
            return unwrapped_values, "Nullable(Nothing)" if has_none else "Nothing"
        if all(isinstance(value, int) and not isinstance(value, bool) for value in present_values):
            written_values, held_type = unwrapped_values, cls.get_integer_type(present_values, path)
        elif all(isinstance(value, Decimal) for value in present_values):
            written_values, held_type = unwrapped_values, cls.get_decimal_type(present_values, path)
        else:
            written_values = []
            held_type = "Nothing"
            for value in unwrapped_values:
                if value is None:
                    written_values.append(None)
                    continue
                written_value, value_type = cls.get_written_value(value, path)
                written_values.append(written_value)
                merged_type = ClickhouseTypeNames.get_merged_type(held_type, value_type)
                if merged_type is None:
                    raise ValidationError(
                        f"{path}: the values a list or a dict holds are of one type, got {held_type} and {value_type}"
                    )
                held_type = merged_type
        if not has_none:
            return written_values, held_type
        if held_type.startswith(("Array(", "Tuple(", "Map(")):
            raise ValidationError(f"{path}: a None among lists, tuples or dicts - ClickHouse holds no NULL of them")
        return written_values, f"Nullable({held_type})"

    @staticmethod
    def get_integer_type(values: list[int], path: str) -> str:
        """The first integer type holding every value.

        Args:
            values: The ints.
            path: Where the values are, named in an error.

        Returns:
            The type.

        Raises:
            ValidationError: No integer type holds them all.
        """
        low, high = min(values), max(values)
        for type_name, type_low, type_high in CLICKHOUSE_DYNAMIC_INTEGER_TYPES:
            if type_low <= low and high <= type_high:
                return type_name
        raise ValidationError(f"{path}: {low if low < 0 else high} is outside every ClickHouse integer type")

    @staticmethod
    def get_decimal_type(values: list[Decimal], path: str) -> str:
        """``Decimal(P, S)`` holding every value - the most digits before and after the point.

        Args:
            values: The decimals.
            path: Where the values are, named in an error.

        Returns:
            The type.

        Raises:
            ValidationError: A decimal isn't finite, or they need more digits than a decimal type holds.
        """
        integer_digits = 0
        scale = 0
        for value in values:
            if not value.is_finite():
                raise ValidationError(f"{path}: {value} has no ClickHouse decimal")
            _, digits, exponent = value.as_tuple()
            exponent = cast("int", exponent)
            integer_digits = max(integer_digits, len(digits) + exponent)
            scale = max(scale, -exponent)
        precision = max(integer_digits + scale, 1)
        if precision > CLICKHOUSE_DECIMAL_MAX_PRECISION:
            raise ValidationError(
                f"{path}: a decimal of {precision} digits - a ClickHouse decimal holds "
                f"{CLICKHOUSE_DECIMAL_MAX_PRECISION} at most"
            )
        return f"Decimal({precision}, {scale})"

    @classmethod
    def to_python(cls, field: Field[Any], value: Any) -> Any:
        """The Python value the driver returned - a moment without a time zone as one in UTC, at any
        depth.

        Args:
            field: The dynamic field.
            value: The value.

        Returns:
            The value.
        """
        return cls.get_python_value(value)

    @classmethod
    def get_python_value(cls, value: Any) -> Any:
        """A value read - a moment without a time zone as one in UTC, at any depth.

        Args:
            value: The value.

        Returns:
            The value.
        """
        if isinstance(value, datetime.datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
        if isinstance(value, list):
            return [cls.get_python_value(element) for element in value]
        if isinstance(value, tuple):
            return tuple(cls.get_python_value(element) for element in value)
        if isinstance(value, dict):
            return {key: cls.get_python_value(item) for key, item in value.items()}
        return value
