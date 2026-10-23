from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from hare.sql import SqlContext, functions
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone


class ConcatFunction(functions.Concat):
    """``CONCAT`` of arguments - a Python literal as its text: a bool as 'true'/'false', a float as
    ``repr()`` writes it, a Decimal with every digit it has and no exponent, a date/datetime/time as ISO text;
    the dialect types what is left."""

    @staticmethod
    def format_temporal_literal(value: date) -> str:
        """Text a date/datetime literal is concatenated as - the same on every dialect.

        Args:
            value: The literal; an aware datetime is converted to UTC first - without time zone
                support (``use_timezone=False``) to the local wall clock a naive value is written as.

        Returns:
            The ISO-formatted text.
        """
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = (
                    value.astimezone(UTC) if Timezone.get_use_timezone() else Timezone.get_system_local_naive(value)
                )
            return value.isoformat(" ")
        return value.isoformat()

    @classmethod
    def get_arg_sql(cls, arg: Any, sql_context: SqlContext) -> str:
        if isinstance(arg, ValueWrapper) and isinstance(arg.value, (bool, date, time, float, Decimal)):
            # Rendered as its text - the parameter holds no value of the literal itself.
            if sql_context.parameterizer is not None:
                sql_context.parameterizer.record_literal(arg)
            if isinstance(arg.value, bool):
                arg = ValueWrapper("true" if arg.value else "false")
            elif isinstance(arg.value, date):
                arg = ValueWrapper(cls.format_temporal_literal(arg.value))
            elif isinstance(arg.value, time):
                arg = ValueWrapper(arg.value.isoformat())
            elif isinstance(arg.value, Decimal):
                arg = ValueWrapper(format(arg.value, "f"))
            else:
                arg = ValueWrapper(repr(arg.value))
        sql = arg.get_sql(sql_context.copy(with_alias=False)) if hasattr(arg, "get_sql") else str(arg)
        return sql_context.dialect.renderers.get_concatenated_argument_sql(sql, arg)
