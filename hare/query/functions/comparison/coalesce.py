from typing import Any

from hare.fields.base.field import Field
from hare.query.expressions import Expression, Function
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import COALESCE_NUMERIC_FIELD_CLASSES
from hare.sql import functions
from hare.sql.terms.base.term import Term


class Coalesce(Function):
    """Provides a default value if field is null, e.g. ``Coalesce("field_name", default_value)``."""

    database_func = functions.Coalesce
    # The result is decoded by the first argument's field.
    populate_field_object = True

    def _get_collation_argument(self, argument: ExpressionResult) -> ExpressionResult:
        """Casts a DecimalField column's collated text to NUMERIC, like every other number the
        COALESCE can return - SQLite orders any text above every number.

        Args:
            argument: The resolved argument.

        Returns:
            `argument`, or a copy whose term is the numeric cast.
        """
        if not functions.Collate.is_decimal_text(argument.term):
            return argument
        return ExpressionResult(
            term=functions.NumericCast(functions.Collate.strip(argument.term)),
            joins=argument.joins,
            output_field=argument.output_field,  # type:ignore[call-overload]
        )

    @staticmethod
    def _default_is_compatible(output_field: Field[Any], default_value: Any, default_result: ExpressionResult) -> bool:
        """Whether a default value fits the main argument's field. Ruled out only for a numeric field
        given a default wider than it holds.

        Args:
            output_field: The main argument's output field.
            default_value: The original default value.
            default_result: The default value resolved.

        Returns:
            False when the field's type can't represent the default.
        """
        return OutputFieldCompatibility.is_value_compatible(
            output_field,
            default_value,
            default_result.output_field,  # type:ignore[call-overload]
        )

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """The common type of every argument - see OutputFieldCompatibility.get_common_output_field().

        Args:
            function_arg: The main argument's resolve result.
            default_results: Every default value's resolve result.

        Returns:
            The field the result is decoded through, or None for the raw driver value.
        """
        main_field = function_arg.output_field  # type:ignore[call-overload]
        branches = [
            ResultBranch.of(self.field, main_field)
            if isinstance(self.field, (Expression, Term))
            else ResultBranch(self.field, main_field, is_literal=False)
        ]
        branches.extend(
            ResultBranch.of(default_value, default_result.output_field)  # type:ignore[call-overload]
            for default_value, default_result in zip(self.default_values, default_results, strict=True)
        )
        return OutputFieldCompatibility.get_common_output_field(branches)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        # A default that doesn't fit the main field is cast to its own SQL type - Postgres would
        # otherwise give the parameter the other argument's type, and Decimal("5.55") next to an
        # integer column would come back as 5.
        output_field = function_arg.output_field  # type:ignore[call-overload]
        terms: list[Any] = []
        for default_value, default_result in zip(self.default_values, default_results, strict=True):
            term = default_result.term
            literal_value = default_value.value if isinstance(default_value, Value) else default_value
            if (
                output_field is not None
                and not isinstance(literal_value, (Expression, Term))
                and not self._default_is_compatible(output_field, default_value, default_result)
                and (cast_sql_type := Value.get_cast_sql_type(literal_value, Value.NUMBER_LITERAL_SQL_TYPE))
                is not None
            ):
                term = functions.Cast(term, cast_sql_type)
            elif output_field is None or isinstance(
                self._get_effective_field_object(output_field), COALESCE_NUMERIC_FIELD_CLASSES
            ):
                term = expression_context.dialect.get_decimal_value_term(term)
            terms.append(term)
        return terms
