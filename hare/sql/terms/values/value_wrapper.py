from __future__ import annotations

import json
import uuid
from datetime import date, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.sql.enums import DatePart
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

from hare.sql.terms.term import Term


class ValueWrapper(Term):
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
        return self.get_formatted_value(self.value, sql_context)

    @classmethod
    def get_formatted_value(cls, value: Any, sql_context: SqlContext) -> str:
        quote_char = sql_context.secondary_quote_char or ""

        # Ordered by frequency. An Enum is checked before str: a str-mixin member renders as
        # "Class.member". quote_text() doubles embedded quotes.
        if isinstance(value, Enum):
            if isinstance(value, DatePart):
                return value.value
            return cls.get_formatted_value(value.value, sql_context)
        if isinstance(value, str):
            return SqlContext.quote_text(value, quote_char)
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
            return SqlContext.quote_text(json.dumps(value), quote_char)
        if isinstance(value, (int, float, Decimal)):
            return str(value)
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
