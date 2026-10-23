from __future__ import annotations

from typing import Any, ClassVar

from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.numeric.unrounded_decimal_field import UnroundedDecimalField
from hare.query.functions.math.enums import MathResultType
from hare.query.functions.math.math_function import MathFunction
from hare.sql.terms.functions.decimal_mod import DecimalMod
from hare.sql.terms.functions.function import Function


class Mod(MathFunction):
    """The remainder of ``Mod(dividend, divisor)``, with the dividend's sign, as the ``%`` operator's - an
    integer of two integers, an exact Decimal at the larger of the scales of a Decimal, a float of a
    float."""

    function_name: ClassVar[str] = "MOD"
    output: ClassVar[MathResultType] = MathResultType.REMAINDER
    arity: ClassVar[int] = 2

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        fields = [result.output_field for result in (function_arg, *default_results)]
        numeric_types: list[NumericType] = []
        for field in fields:
            numeric_type = NumericTyping.get_field_type(field)
            if numeric_type is None:
                return None
            numeric_types.append(numeric_type)
        return NumericTyping.get_common_output_field(numeric_types, [])

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # A Decimal remainder is the % operator's exact one - a dialect's own MOD of two Decimals of
        # different scales may read them at one scale.
        result = super().get_result(expression_context)
        if not isinstance(result.term, Function):
            return result
        dividend, divisor = result.term.args
        output_field: Field[Any] | None = result.output_field
        effective_field = None if output_field is None else GeneratedField.get_effective_field(output_field)
        if isinstance(effective_field, DecimalField):
            scale = None if isinstance(effective_field, UnroundedDecimalField) else effective_field.decimal_places
            result.term = DecimalMod(dividend, divisor, scale)
        return result
