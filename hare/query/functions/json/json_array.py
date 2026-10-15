from __future__ import annotations

from typing import Any

from hare.query.expressions import Function
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.query.functions.json.json_object import JSONObject
from hare.sql.functions.json.json_array import JsonArray as JsonArrayTerm


class JSONArray(JSONObject):
    """A JSON array of the given values, decoded as a ``list``: ``JSONArray("name", F("price") * 2,
    5)`` - a string is a field name. Every backend writes a value as Postgres's jsonb holds it."""

    def __init__(self, *values: Any) -> None:
        """
        Args:
            values: The array's items - field names, expressions or literals.
        """
        self.keys = ()
        expressions = [FunctionArguments.get_expression(value) for value in values]
        first_value, *other_values = expressions or [None]
        Function.__init__(self, first_value, *other_values)

    def get_plan_options(self) -> tuple[Any, ...]:
        return ()

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        value_terms, joins = self.get_json_value_terms(expression_context)
        self.field_object = self.OUTPUT_FIELD  # type: ignore[call-overload]
        return ExpressionResult(term=JsonArrayTerm(*value_terms), joins=joins, output_field=self.OUTPUT_FIELD)  # type: ignore[call-overload]


__all__ = ["JSONArray"]
