from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from hare.exceptions import FieldError
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.data.text.char_field import CharField
from hare.fields.field import Field
from hare.query.expressions import Expression, Function
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import CastType
from hare.sql.functions.cast_to import CastTo
from hare.sql.terms.term import Term
from hare.time import Timezone


class Cast(Function):
    """Converts a value to ``output_field``'s type with Postgres's rules on every backend - a float
    rounds half to even to an integer, a Decimal half away from zero, and text that isn't a value of
    the type raises ``OperationalError``: ``Cast("price", IntField())``,
    ``Cast("code", CharField(max_length=3))``."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("target", PlanPartType.NONE),
        ("cast_output_field", PlanPartType.NONE),
    )

    populate_field_object = True

    def __init__(self, expression: str | int | float | Decimal | Expression | Term, output_field: Field[Any]) -> None:
        """
        Args:
            expression: A field name, an expression or a literal.
            output_field: An integer, float, Decimal, text, boolean, date, datetime or time field.

        Raises:
            FieldError: ``output_field`` is of another type.
        """
        self.target = FunctionArguments.get_cast_type(output_field)
        if self.target == CastType.UNKNOWN or not isinstance(output_field, Field):
            raise FieldError(f"Cast() can't convert to {type(output_field).__name__}")
        self.cast_output_field = output_field
        super().__init__(FunctionArguments.get_expression(expression))

    def get_parameters(self) -> tuple[int | None, int | None]:
        """An integer target's bits, a Decimal's digits and places, a text's max length."""
        field = self.cast_output_field
        if self.target == CastType.INTEGER:
            return (16 if isinstance(field, SmallIntField) else 64 if isinstance(field, BigIntField) else 32, None)
        if isinstance(field, DecimalField):
            return field.max_digits, field.decimal_places
        if isinstance(field, CharField):
            return field.max_length, None
        return None, None

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.target, type(self.cast_output_field), self.get_parameters())

    def _get_function_field(self, term: Term | str, *default_values: Any) -> CastTo:
        return CastTo(term, self.cast_output_field, self.target, CastType.UNKNOWN, self.get_parameters())

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        cast_term = result.term
        if isinstance(cast_term, CastTo):
            cast_term.source = FunctionArguments.get_cast_type(result.output_field)  # type:ignore[call-overload]
            cast_term.is_aware = Timezone.get_use_timezone()
        self.field_object = self.cast_output_field
        return ExpressionResult(term=cast_term, joins=result.joins, output_field=self.cast_output_field)

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        return self.cast_output_field
