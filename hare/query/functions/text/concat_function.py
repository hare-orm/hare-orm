from datetime import UTC, date, datetime

from hare.sql import SqlContext, functions
from hare.sql.terms.base.value_wrapper import ValueWrapper


class ConcatFunction(functions.Concat):
    """``CONCAT`` of arguments - a bool literal is concatenated as 'true'/'false' and a
    date/datetime literal as ISO text on every dialect; the dialect types what is left."""

    @staticmethod
    def format_temporal_literal(value: date) -> str:
        """Text a date/datetime literal is concatenated as - the same on every dialect.

        Args:
            value: The literal; an aware datetime is converted to UTC first.

        Returns:
            The ISO-formatted text.
        """
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(UTC)
            return value.isoformat(" ")
        return value.isoformat()

    @classmethod
    def get_arg_sql(cls, arg, ctx: SqlContext):
        if isinstance(arg, ValueWrapper) and isinstance(arg.value, bool):
            return functions.BooleanAsText(arg).get_sql(ctx.copy(with_alias=False))
        if isinstance(arg, ValueWrapper) and isinstance(arg.value, (date, float)):
            # Rendered as its text - the parameter holds no value of the literal itself.
            if ctx.parameterizer is not None:
                ctx.parameterizer.record_literal(arg)
            if isinstance(arg.value, date):
                arg = ValueWrapper(cls.format_temporal_literal(arg.value))
            else:
                arg = ValueWrapper(functions.NumberText.format_float(arg.value))
        sql = arg.get_sql(ctx.copy(with_alias=False)) if hasattr(arg, "get_sql") else str(arg)
        return ctx.dialect.get_concatenated_argument_sql(sql, arg)
