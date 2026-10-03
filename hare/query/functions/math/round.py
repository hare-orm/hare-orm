from typing import Any

from hare.exceptions import QueryError
from hare.fields.base.field import Field
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.query.expressions import Expression, Function
from hare.query.expressions.base.value import Value
from hare.query.expressions.constants import DECIMAL_OUTPUT_FIELD_MAX_DIGITS
from hare.sql import functions
from hare.sql.terms.base.term import Term


class Round(Function):
    """Rounds a number to ``precision`` decimal places in the database, e.g.
    ``Round(F("price") / 3, 2)``. A Decimal result keeps exactly ``precision`` decimal places on
    every backend; an integer stays an integer and a float a float. Postgres rounds the exact
    numeric value; SQLite rounds its double, so a value with no exact binary form (``2.675``) can
    land on the other side of the tie there."""

    database_func = functions.Round
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
                f"Round() precision must be between 0 and {DECIMAL_OUTPUT_FIELD_MAX_DIGITS}, got {precision}"
            )
        super().__init__(field)  # type: ignore[arg-type]
        self.precision = precision

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.precision,)

    def _get_function_field(self, field: Term | str, *default_values: Any) -> functions.Round:
        return functions.Round(field, self.precision)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        """A Decimal result is decoded with exactly ``precision`` places; other types keep theirs.

        Args:
            field_object: The rounded value's field.

        Returns:
            The field the rounded result is decoded through.
        """
        if isinstance(self._get_effective_field_object(field_object), DecimalField):
            decimal_output_field = Value.get_decimal_output_field(self.precision)
            if decimal_output_field is not None:
                return decimal_output_field
        return field_object
