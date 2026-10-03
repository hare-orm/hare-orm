from __future__ import annotations

import json
import uuid
from datetime import date, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.enums import DatePart

if TYPE_CHECKING:
    from typing import Self

from hare.sql.terms.base.term import Term


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

    def get_value_sql(self, ctx: SqlContext) -> str:
        return self.get_formatted_value(self.value, ctx)

    @classmethod
    def get_formatted_value(cls, value: Any, ctx: SqlContext) -> str:
        quote_char = ctx.secondary_quote_char or ""

        # Ordered by frequency. An Enum is checked before str: a str-mixin member renders as
        # "Class.member". quote_text() doubles embedded quotes.
        if isinstance(value, Enum):
            if isinstance(value, DatePart):
                return value.value
            return cls.get_formatted_value(value.value, ctx)
        if isinstance(value, str):
            return SqlContext.quote_text(value, quote_char)
        if isinstance(value, bool):
            return str(value).lower()
        if value is None:
            return "null"
        if isinstance(value, Term):
            return value.get_sql(ctx)
        if isinstance(value, (date, time)):
            return cls.get_formatted_value(value.isoformat(), ctx)
        if isinstance(value, uuid.UUID):
            return cls.get_formatted_value(str(value), ctx)
        if isinstance(value, (dict, list)):
            return SqlContext.quote_text(json.dumps(value), quote_char)
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls.get_formatted_binary_value(bytes(value), ctx)
        raise TypeError(f"Can't render a {type(value).__name__} value as an SQL literal")

    @staticmethod
    def get_formatted_binary_value(value: bytes, ctx: SqlContext) -> str:
        """Renders bytes as the dialect's hexadecimal binary literal.

        Args:
            value: The bytes.
            ctx: The SQL rendering context.

        Returns:
            The literal.
        """
        return ctx.dialect.get_bytes_literal_sql(value)

    def get_sql(
        self,
        ctx: SqlContext,
    ) -> str:
        parameterizer = ctx.parameterizer
        if parameterizer is None or not parameterizer.should_parameterize(self.value) or not self.allow_parametrize:
            if parameterizer is not None:
                parameterizer.record_literal(self.parameter_source or self)
            sql = self.get_value_sql(ctx)
            return ctx.format_alias_sql(sql, self.alias)

        param = parameterizer.create_param(
            self.value, self.parameter_source or self, reuse=ctx.dialect.numbers_parameters
        )
        param_sql = param.get_sql(ctx)
        if isinstance(self.value, bool):
            # A bare parameter has no context to type it (a CASE of literals) - cast explicitly.
            param_sql = ctx.dialect.get_cast_parameter_sql(param_sql, self.value)
        return ctx.format_alias_sql(param_sql, self.alias)
