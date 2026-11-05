from __future__ import annotations

import datetime
from typing import Any

from hare.dialects.enums import ParameterPosition
from hare.fields.field import Field
from hare.query.expressions import Expression, Function
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value import Value
from hare.sql import functions
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class Coalesce(Function):
    """The first argument that isn't NULL, read using the common compatible type of all arguments:
    ``Coalesce("nickname", F("name"), "-")``. Strings after the first argument are literal values."""

    database_function = functions.Coalesce
    default_value_counts = (1, None)
    returns_an_argument = True
    populate_field_object = True

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
        main_field = function_arg.output_field
        branches = [
            ResultBranch.of(self.field, main_field)
            if isinstance(self.field, (Expression, Term))
            else ResultBranch(self.field, main_field, is_literal=False)
        ]
        branches.extend(
            ResultBranch.of(default_value, default_result.output_field)
            for default_value, default_result in zip(self.default_values, default_results, strict=True)
        )
        OutputFieldCompatibility.raise_if_value_types_mix(branches, "Coalesce()")
        return OutputFieldCompatibility.get_common_output_field(branches)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        if isinstance(result.term, functions.Coalesce):
            renderers = expression_context.dialect.renderers
            output_field: Field[Any] | None = result.output_field
            result.term.args = [renderers.get_result_branch_term(arg, output_field) for arg in result.term.args]
        return result

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        # A default that doesn't fit the main field is cast to its own SQL type - a database would
        # otherwise give the parameter the other argument's type, and Decimal("5.55") next to an
        # integer column would come back as 5.
        output_field = function_arg.output_field
        # A bare literal first - Value(None) - types nothing: a database would read every parameter as text.
        types_nothing = isinstance(function_arg.term, ValueWrapper)
        terms: list[Any] = []
        for default_value, default_result in zip(self.default_values, default_results, strict=True):
            term = default_result.term
            literal_value = default_value.value if isinstance(default_value, Value) else default_value
            if (
                literal_value is not None
                and not isinstance(literal_value, (Expression, Term))
                and self.is_json_field(output_field)
            ):
                # Bound as the JSON column binds a value - its JSON text, which takes the column's type.
                terms.append(term)
                continue
            if types_nothing and isinstance(term, ValueWrapper):
                term = Value.get_typed_term(
                    term, term.value, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect
                )
            elif (
                output_field is not None
                and not isinstance(literal_value, (Expression, Term))
                and not OutputFieldCompatibility.is_value_compatible(
                    output_field, default_value, default_result.output_field
                )
                and (cast_sql_type := Value.get_cast_sql_type(literal_value, Value.NUMBER_LITERAL_SQL_TYPE))
                is not None
            ):
                term = functions.Cast(term, cast_sql_type)
            elif isinstance(literal_value, datetime.date) and not isinstance(literal_value, datetime.datetime):
                # A date typed as one - beside a moment the parameter would take the moment's type, which a
                # driver may fill with the date's midnight in the machine's time zone.
                term = Value.get_typed_term(
                    term, literal_value, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect
                )
            terms.append(term)
        return terms
