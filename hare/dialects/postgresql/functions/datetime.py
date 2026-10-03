from typing import Any, ClassVar

from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.query.expressions.base.constant_expression import ConstantExpression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.sql.terms.base.literal_value import LiteralValue


class TransactionNow(ConstantExpression):
    """The moment the current transaction started - ``CURRENT_TIMESTAMP``, the same for every
    statement of the transaction, where ``Now()`` is the moment of each statement.

    Example: ``Event.objects.filter(created_at__lt=TransactionNow())``
    """

    #: Shared, long-lived instance - the statement plans hold output fields weakly.
    OUTPUT_FIELD: ClassVar[DatetimeField[Any]] = DatetimeField()

    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(term=LiteralValue("CURRENT_TIMESTAMP"), output_field=self.OUTPUT_FIELD)  # type: ignore[call-overload]

    value_field = OUTPUT_FIELD
