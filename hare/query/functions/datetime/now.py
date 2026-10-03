from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.db_defaults.now import Now as NowDefault
from hare.query.expressions.base.constant_expression import ConstantExpression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.sql.terms.base.literal_value import LiteralValue


class Now(ConstantExpression):
    """The current moment, as the database sees it - a ``DatetimeField`` value: ``filter(
    expires_at__lt=Now())``, ``update(seen_at=Now())``."""

    #: Shared, long-lived instance - see ``Extract.DATE_PART_OUTPUT_FIELD``.
    NOW_OUTPUT_FIELD = DatetimeField()

    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(
            term=LiteralValue(NowDefault().get_sql(expression_context.dialect)),
            output_field=self.NOW_OUTPUT_FIELD,  # type:ignore[call-overload]
        )

    value_field = NOW_OUTPUT_FIELD
