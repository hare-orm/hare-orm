from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions import F
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class OffsetWindowFunction(WindowFunction):
    """Base for ``LAG``/``LEAD`` - a field value read from a neighboring row."""

    field: str

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("get_plan_options", PlanPartType.KEY_METHOD),
        ("field", PlanPartType.FIELD),
        ("offset", PlanPartType.ARGUMENT),
        ("default", PlanPartType.ARGUMENT),
        # Follows from the default.
        ("accepts_encrypted_field", PlanPartType.NONE),
    )

    def __init__(self, field: str, offset: int = 1, default: Any = None) -> None:
        self.field = field
        self.offset = offset
        self.default = default
        # A default is a plaintext value the column's decoder would try to decrypt.
        self.accepts_encrypted_field = default is None

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], Field[Any] | None]:
        field_result = F(self.field).get_result(expression_context)
        self._raise_if_encrypted_field(field_result.output_field)  # type: ignore[call-overload]
        output_field = field_result.output_field  # type: ignore[call-overload]
        default_encoder = (
            None
            if output_field is None
            else partial(self.get_database_default, output_field, expression_context.dialect)
        )
        offset = ExpressionArguments.get_result(self, "offset", self.offset, expression_context)
        default = ExpressionArguments.get_result(
            self, "default", self.default, expression_context, encoder=default_encoder
        )
        term = self.get_analytic_term(field_result.term, offset.term, default.term)
        joins = ExpressionResult.dedup_joins(field_result.joins, offset.joins, default.joins)
        return term, joins, output_field

    def get_database_default(self, output_field: Field[Any], dialect: Dialect, default: Any) -> Any:
        """Encodes a default through the read field, like a value written to that column.

        Args:
            output_field: The read field.
            dialect: The dialect the value is encoded for.
            default: The raw default value.

        Returns:
            The database value of the default.

        Raises:
            QueryError: If the default can't be decoded through the read field without losing
                information (e.g. a fractional default for an integer field).
        """
        if default is None:
            return None
        if not OutputFieldCompatibility.is_value_compatible(output_field, default, None):
            raise QueryError(
                f"{type(self).__name__}({self.field!r}) default {default!r} doesn't fit the field's own type "
                f"{type(output_field).__name__} - the result would lose it when decoded"
            )
        return dialect.types.get_db_value(output_field, default, output_field.model)
