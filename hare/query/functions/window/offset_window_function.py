from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions import Expression, F
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class OffsetWindowFunction(WindowFunction):
    """Base for ``LAG``/``LEAD`` - a field value read from a neighboring row."""

    field: str

    def __init__(self, field: str, offset: int = 1, default: Any = None) -> None:
        self.field = field
        self.offset = offset
        self.default = default
        # A default is a plaintext value the column's decoder would try to decrypt.
        self.accepts_encrypted_field = default is None

    def get_plan_arguments(self, context: PlanContext) -> Iterable[PlanDescription | None]:
        """The field read, then the offset and the default - a literal one by its type; a None
        default renders ``NULL``, not a parameter, and keeps no plan."""
        return (
            self._get_field_plan_description(context),
            Expression.get_argument_plan_description(self.offset, context),
            Expression.get_argument_plan_description(self.default, context),
        )

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        field_result = F(self.field).get_result(expression_context)
        self._raise_if_encrypted_field(field_result.output_field)  # type: ignore[call-overload]
        output_field = field_result.output_field  # type: ignore[call-overload]
        default_encoder = (
            None
            if output_field is None
            else partial(self.get_database_default, output_field, expression_context.dialect)
        )
        database_default = self.default if default_encoder is None else default_encoder(self.default)
        term = self.get_analytic_term(field_result.term, self.offset, database_default)
        # The term's arguments are the expression, the offset and the default. A None default is a
        # NULL term and isn't recorded.
        if expression_context.value_wrapper_refs is not None:
            offset_arg, default_arg = term.args[1:]
            if isinstance(offset_arg, ValueWrapper):
                expression_context.value_wrapper_refs.append((ValueRefOrigin.ANNOTATION, LiteralValueRef(offset_arg)))
            if isinstance(default_arg, ValueWrapper):
                expression_context.value_wrapper_refs.append(
                    (ValueRefOrigin.ANNOTATION, LiteralValueRef(default_arg, encoder=default_encoder))
                )
        return term, field_result.joins, output_field

    def get_database_default(self, output_field: "Field[Any]", dialect: Dialect, default: Any) -> Any:
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
