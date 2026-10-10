from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from hare.classes.class_path import ClassPath
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DICTIONARY_DEFAULT_FUNCTION,
    CLICKHOUSE_DICTIONARY_FUNCTION,
    CLICKHOUSE_TUPLE_FUNCTION,
)
from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F, Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.literal_value import LiteralValue


class DictGet(Expression):
    """``dictGet(dictionary, attribute, key)`` - a value of a ClickHouse dictionary by its key, read
    from the server's memory in place of a JOIN::

        Visit.objects.annotate(
            country=DictGet("country_names", "name", "country_code", output_field=fields.CharField(max_length=50))
        )

    Args:
        dictionary: The dictionary's name.
        attribute: The attribute read - the column of a field the dictionary names.
        key: The key looked up - a field name or an expression; a sequence of them for a dictionary
            keyed by several fields.
        output_field: The field the value is read as.
        default: The value of a key the dictionary doesn't hold - a literal or an expression
            (``dictGetOrDefault``); without one the server gives the attribute type's own default.

    Raises:
        QueryError: ``dictionary`` or ``attribute`` isn't a non-empty string, ``key`` is empty,
            ``output_field`` isn't a field.
    """

    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("dictionary", PlanPartType.KEY),
        ("attribute", PlanPartType.KEY),
        ("keys", PlanPartType.EXPRESSIONS),
        ("default", PlanPartType.EXPRESSION),
        ("get_output_field_type", PlanPartType.KEY_METHOD),
        ("output_field", PlanPartType.NONE),
    )

    def __init__(
        self,
        dictionary: str,
        attribute: str,
        key: str | Expression | Sequence[str | Expression],
        *,
        output_field: Field[Any],
        default: Any = None,
    ) -> None:
        for argument_name, argument in (("dictionary", dictionary), ("attribute", attribute)):
            if not isinstance(argument, str) or not argument:
                raise QueryError(f"DictGet({argument_name}=...) takes a non-empty string, got {argument!r}")
        if not isinstance(output_field, Field):
            raise QueryError(f"DictGet(output_field=...) takes a field, got {output_field!r}")
        key_parts = [key] if isinstance(key, str | Expression) else list(key)
        if not key_parts:
            raise QueryError("DictGet(key=...) takes a field name, an expression, or a sequence of them")
        self.dictionary = dictionary
        self.attribute = attribute
        self.keys: tuple[Expression, ...] = tuple(
            F(key_part) if isinstance(key_part, str) else key_part for key_part in key_parts
        )
        self.output_field = output_field
        self.default: Expression | None = (
            None if default is None else default if isinstance(default, Expression) else Value(default)
        )

    def __repr__(self) -> str:
        return f"DictGet({self.dictionary!r}, {self.attribute!r}, {list(self.keys)!r})"

    def get_output_field_type(self) -> str:
        """The class of the field the value is read as - two expressions of one SQL text read their
        values differently by it."""
        return ClassPath.get(type(self.output_field))

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        literals = expression_context.dialect.literals
        key_results = [key.get_result(expression_context) for key in self.keys]
        joins = [join for key_result in key_results for join in key_result.joins]
        key_term = (
            key_results[0].term
            if len(key_results) == 1
            else Function(CLICKHOUSE_TUPLE_FUNCTION, *(key_result.term for key_result in key_results))
        )
        arguments = [
            LiteralValue(literals.get_string_literal_sql(self.dictionary)),
            LiteralValue(literals.get_string_literal_sql(self.attribute)),
            key_term,
        ]
        function_name = CLICKHOUSE_DICTIONARY_FUNCTION
        if self.default is not None:
            default_result = self.default.get_result(expression_context)
            joins.extend(default_result.joins)
            arguments.append(default_result.term)
            function_name = CLICKHOUSE_DICTIONARY_DEFAULT_FUNCTION
        return ExpressionResult(term=Function(function_name, *arguments), joins=joins, output_field=self.output_field)

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        return self.output_field
