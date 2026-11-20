from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from hare.dialects.postgresql.lookups.constants import (
    POSTGRES_ARRAY_ELEMENT_TYPE_BY_PYTHON_TYPE,
    POSTGRES_AWARE_DATETIME_ARRAY_ELEMENT_TYPE,
    POSTGRES_BIGINT_MAX,
    POSTGRES_BIGINT_MIN,
    POSTGRES_NAIVE_DATETIME_ARRAY_ELEMENT_TYPE,
    POSTGRES_WIDE_INTEGER_ARRAY_ELEMENT_TYPE,
)
from hare.sql.functions.cast import Cast
from hare.sql.terms.array import Array
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.term import Term


class ArrayParameter(Cast, ListParameter):
    """``CAST($1 AS type[])`` - a value list bound as ONE array parameter, typed by the compared field
    or, when none is known, by the values themselves.

    Args:
        values: The array elements.
        element_type: The element SQL type.
        typed_by_values: Whether ``element_type`` was taken from the values.
    """

    def __init__(self, values: list[Any], element_type: str, *, typed_by_values: bool = False) -> None:
        # Array (not a plain ParameterizedValueWrapper) - it binds the whole list as ONE parameter
        # when a parameterizer is present, and renders a valid `ARRAY[...]` literal for
        # `.sql(parameters_inline=True)` when there isn't one.
        super().__init__(Array(*values), f"{element_type}[]")
        self.element_type = element_type
        self.typed_by_values = typed_by_values

    def get_parameter_source(self) -> Term:
        return self.args[0]

    def get_parameter_encoder(self) -> Callable[[list[Any]], list[Any] | None]:
        return partial(self.encode_parameter, element_type=self.element_type, typed_by_values=self.typed_by_values)

    @classmethod
    def encode_parameter(cls, values: list[Any], *, element_type: str, typed_by_values: bool) -> list[Any] | None:
        """Encode a fresh list using the declared array type without retaining a SQL array."""
        if cls.holds_term(values):
            return None
        if typed_by_values and cls.get_array_element_type(values) != element_type:
            return None
        return values

    @staticmethod
    def holds_term(values: Sequence[Any]) -> bool:
        """Whether one of ``values`` is a SQL term (the CAST an IntField lookup puts around a
        non-integer number) - it can't be an element of one bound array parameter."""
        for value_type in set(map(type, values)):
            if issubclass(value_type, Term):
                return True
        return False

    @classmethod
    def get_array_element_type(cls, values: Sequence[Any]) -> str | None:
        """The array element SQL type every one of ``values`` binds as.

        Args:
            values: The non-None lookup values.

        Returns:
            The SQL type, or None when the values have no single common type.
        """
        value_types = {type(value) for value in values}
        if len(value_types) == 1:
            (value_type,) = value_types
            if value_type is int:
                if min(values) >= POSTGRES_BIGINT_MIN and max(values) <= POSTGRES_BIGINT_MAX:
                    return POSTGRES_ARRAY_ELEMENT_TYPE_BY_PYTHON_TYPE[int]
            elif not issubclass(value_type, (int, datetime.datetime)):
                # The type alone decides.
                return cls.get_value_array_element_type(values[0])
        element_types = {cls.get_value_array_element_type(value) for value in values}
        if len(element_types) != 1:
            return None
        return element_types.pop()

    @classmethod
    def get_value_array_element_type(cls, value: Any) -> str | None:
        """The array element SQL type one value binds as.

        Args:
            value: A non-None lookup value.

        Returns:
            The SQL type, or None for a value of no known type.
        """
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                return POSTGRES_NAIVE_DATETIME_ARRAY_ELEMENT_TYPE
            return POSTGRES_AWARE_DATETIME_ARRAY_ELEMENT_TYPE
        for python_type in type(value).__mro__:
            element_type = POSTGRES_ARRAY_ELEMENT_TYPE_BY_PYTHON_TYPE.get(python_type)
            if element_type is None:
                continue
            if python_type is int and not POSTGRES_BIGINT_MIN <= value <= POSTGRES_BIGINT_MAX:
                return POSTGRES_WIDE_INTEGER_ARRAY_ELEMENT_TYPE
            return element_type
        return None
