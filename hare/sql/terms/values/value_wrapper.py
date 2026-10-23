from __future__ import annotations

import json
import uuid
from datetime import date, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.dialects.base.literals.constants import SQL_LONG_INT_MESSAGE
from hare.exceptions import ValidationError
from hare.sql.enums import DatePart
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

from hare.sql.terms.term import Term


class ValueWrapper(Term):
    """A Python value of a statement - a bound parameter, or a literal written into the SQL."""

    is_aggregate = None
    #: The wrapper a copy was made from (``as_()``, ``replace_table()``) - its parameter comes from
    #: that wrapper, so a value bound through the original's reference reaches the copy too. None
    #: for a wrapper that is no copy.
    parameter_source: ValueWrapper | None = None

    def __init__(self, value: Any, alias: str | None = None, allow_parametrize: bool = True) -> None:
        """A wrapper for a constant value such as a string or number.

        Args:
            value: The value to be wrapped.
            alias: An optional alias for the value.
            allow_parametrize: Whether the value should be replaced with a parameter in the
                query if a parameterizer is used.
        """
        super().__init__(alias)
        self.value = value
        self.allow_parametrize = allow_parametrize

    def __copy__(self) -> Self:
        copied_wrapper = super().__copy__()
        copied_wrapper.parameter_source = self.parameter_source or self
        return copied_wrapper

    def get_value_sql(self, sql_context: SqlContext) -> str:
        literals = sql_context.dialect.literals
        value = self.value
        if literals.writes_bound_values and value is not None and not isinstance(value, Enum | Term):
            # As the dialect's client binds the value - a view's query keeps its values so.
            return literals.get_literal_sql(value)
        return self.get_formatted_value(value, sql_context)

    @classmethod
    def get_formatted_value(cls, value: Any, sql_context: SqlContext) -> str:
        """A Python value as a literal of the SQL - the dialect's string literal for a text.

        Args:
            value: The value.
            sql_context: How the SQL is written.

        Returns:
            The literal.
        """
        # Ordered by frequency. An Enum is checked before str: a str-mixin member renders as
        # "Class.member". A text is the dialect's string literal - a dialect may read a backslash in
        # one as an escape.
        if isinstance(value, Enum):
            if isinstance(value, DatePart):
                return value.value
            return cls.get_formatted_value(value.value, sql_context)
        if isinstance(value, str):
            return sql_context.dialect.literals.get_string_literal_sql(value)
        if isinstance(value, bool):
            return sql_context.dialect.literals.get_boolean_literal_sql(value)
        if value is None:
            return "null"
        if isinstance(value, Term):
            return value.get_sql(sql_context)
        if isinstance(value, (date, time)):
            return cls.get_formatted_value(value.isoformat(), sql_context)
        if isinstance(value, uuid.UUID):
            return cls.get_formatted_value(str(value), sql_context)
        if isinstance(value, (dict, list)):
            return sql_context.dialect.literals.get_string_literal_sql(json.dumps(value))
        if isinstance(value, (int, float, Decimal)):
            try:
                return str(value)
            except ValueError:
                # An int longer than Python writes as text.
                raise ValidationError(SQL_LONG_INT_MESSAGE) from None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls.get_formatted_binary_value(bytes(value), sql_context)
        raise TypeError(f"Can't render a {type(value).__name__} value as an SQL literal")

    @staticmethod
    def get_formatted_binary_value(value: bytes, sql_context: SqlContext) -> str:
        """Renders bytes as the dialect's hexadecimal binary literal.

        Args:
            value: The bytes.
            sql_context: The SQL rendering context.

        Returns:
            The literal.
        """
        return sql_context.dialect.literals.get_bytes_literal_sql(value)

    def get_sql(
        self,
        sql_context: SqlContext,
    ) -> str:
        parameterizer = sql_context.parameterizer
        if parameterizer is None or not parameterizer.should_parameterize(self.value) or not self.allow_parametrize:
            if parameterizer is not None:
                parameterizer.record_literal(self.parameter_source or self)
            sql = self.get_value_sql(sql_context)
            return sql_context.format_alias_sql(sql, self.alias)

        parameter = parameterizer.create_parameter(
            self.value, self.parameter_source or self, reuse=sql_context.dialect.parameters.numbers_parameters
        )
        parameter_sql = parameter.get_sql(sql_context)
        if isinstance(self.value, bool):
            # A bare parameter has no context to type it (a CASE of literals) - cast explicitly.
            parameter_sql = sql_context.dialect.parameters.get_cast_parameter_sql(parameter_sql, self.value)
        return sql_context.format_alias_sql(parameter_sql, self.alias)
