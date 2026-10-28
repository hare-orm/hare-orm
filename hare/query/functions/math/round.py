from __future__ import annotations

from typing import Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import QueryError
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, Function
from hare.query.expressions.compatibility.constants import NON_NUMBER_VALUE_TYPES
from hare.query.expressions.constants import DECIMAL_OUTPUT_FIELD_MAX_DIGITS
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import functions
from hare.sql.terms.term import Term


class Round(Function):
    """Rounds a number to ``precision`` decimal places in the database, e.g.
    ``Round(F("price") / 3, 2)``, a tie by the database's own rule. A Decimal result keeps exactly
    ``precision`` decimal places; an integer stays an integer and a float a float."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("precision", PlanPartType.NONE),
    )

    database_function = functions.Round
    refused_argument_value_types = NON_NUMBER_VALUE_TYPES
    populate_field_object = True

    def __init__(self, field: str | Expression | Term, precision: int = 0) -> None:
        """
        Args:
            field: The field name or expression to round.
            precision: Decimal places to keep, 0 to ``DECIMAL_OUTPUT_FIELD_MAX_DIGITS``.

        Raises:
            QueryError: If ``precision`` isn't an integer in that range.
        """
        if isinstance(precision, bool) or not isinstance(precision, int):
            raise QueryError(f"Round() precision must be an integer, got {precision!r}")
        if not 0 <= precision <= DECIMAL_OUTPUT_FIELD_MAX_DIGITS:
            raise QueryError(
                f"Round() precision must be between 0 and {DECIMAL_OUTPUT_FIELD_MAX_DIGITS}, got "
                f"{ValueDescriptions.get_shown_text(precision)}"
            )
        super().__init__(field)  # type: ignore[arg-type]
        self.precision = precision

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.precision,)

    def get_function_term(self, term: Term | str, *default_values: Any) -> functions.Round:
        return functions.Round(term, self.precision)

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        """A Decimal result is decoded with exactly ``precision`` places; other types keep theirs.

        Args:
            field_object: The rounded value's field.

        Returns:
            The field the rounded result is decoded through.
        """
        if isinstance(GeneratedField.get_effective_field(field_object), DecimalField):
            decimal_output_field = NumericTyping.get_decimal_output_field(self.precision)
            if decimal_output_field is not None:
                return decimal_output_field
        return field_object
