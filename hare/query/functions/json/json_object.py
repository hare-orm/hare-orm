from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.exceptions import QueryError
from hare.fields.data.binary_field import BinaryField
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Function
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value import Value
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import JsonValueType
from hare.sql.functions.cast import Cast as CastTerm
from hare.sql.functions.json.json_object import JsonObject as JsonObjectTerm
from hare.sql.functions.json.json_value import JsonValue
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class JSONObject(Function):
    """A JSON object of the given keys and values, decoded as a ``dict``:
    ``JSONObject(name="name", total=F("price") * 2)`` - a string is a field name. Every backend
    writes a value as Postgres's jsonb holds it."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("keys", PlanPartType.NONE),
    )

    populate_field_object = True

    #: Shared, long-lived output field - the statement plans hold it weakly.
    OUTPUT_FIELD: JSONField[Any] = JSONField()

    #: Field class to how its value is written into the object, most specific first.
    JSON_VALUE_TYPES: ClassVar[tuple[tuple[type, JsonValueType], ...]] = (
        (BooleanField, JsonValueType.BOOLEAN),
        (DecimalField, JsonValueType.DECIMAL),
        (FloatField, JsonValueType.FLOAT),
        (DatetimeField, JsonValueType.DATETIME),
        (TimeField, JsonValueType.TIME),
        (BinaryField, JsonValueType.BINARY),
        (JSONField, JsonValueType.JSON),
    )

    def __init__(self, **fields: Any) -> None:
        """
        Args:
            fields: The object's keys and their values - field names, expressions or literals.

        Raises:
            QueryError: A key holds a null byte.
        """
        for key in fields:
            if SQL_NULL_BYTE in key:
                raise QueryError(SQL_NULL_BYTE_MESSAGE.format(text=key))
        self.keys = tuple(fields)
        values = [FunctionArguments.get_expression(value) for value in fields.values()]
        first_value, *other_values = values or [None]
        super().__init__(first_value, *other_values)

    def get_plan_options(self) -> tuple[Any, ...]:
        return self.keys

    @classmethod
    def get_json_value_type(cls, field: Field[Any] | None, term: Any) -> JsonValueType:
        """How a value of the field is written into the object - a bytes literal as binary."""
        if isinstance(term, ValueWrapper) and isinstance(term.value, (bytes, bytearray, memoryview)):
            return JsonValueType.BINARY
        effective_field = GeneratedField.get_effective_field(field) if field is not None else None
        for field_class, value_type in cls.JSON_VALUE_TYPES:
            if isinstance(effective_field, field_class):
                return value_type
        return JsonValueType.PLAIN

    @classmethod
    def get_json_value(cls, term: Any, field: Field[Any] | None, dialect: Dialect) -> tuple[Any, JsonValueType]:
        """How a value is written into a JSON value on a dialect - the term and how it's written.

        Args:
            term: The value's term.
            field: The value's field, None where unknown.
            dialect: The dialect.

        Returns:
            The dialect's own JSON text of a column it stores in a form JSON can't hold
            (``TypeMapping.json_term``) as plain text, else the term with its field's type.
        """
        if field is not None and not isinstance(term, ValueWrapper):
            json_term = dialect.types.get_json_term(field, term)
            if json_term is not None:
                return json_term, JsonValueType.PLAIN
        return term, cls.get_json_value_type(field, term)

    def get_json_value_terms(self, expression_context: ExpressionContext) -> tuple[list[Any], list[Any]]:
        """Each value as a JSON value term, in order.

        Args:
            expression_context: The context the values resolve in.

        Returns:
            The terms, and the joins the values need.
        """
        values = [] if self.field is None else [self.field, *self.default_values]
        terms: list[Any] = []
        joins: list[Any] = []
        for value in values:
            result = self._get_collation_argument(
                self._get_argument(expression_context, value, treat_str_as_field=True)
            )
            self._raise_if_encrypted_argument(value, result)
            term, value_type = self.get_json_value(
                result.term,
                result.output_field,  # type: ignore[call-overload]
                expression_context.dialect,
            )
            term = self.get_typed_literal_term(term, value_type, expression_context)
            terms.append(JsonValue(term, value_type, is_aware=Timezone.get_use_timezone()))
            if result.joins:
                joins = ExpressionResult.dedup_joins(joins, result.joins)
        return terms, joins

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        value_terms, joins = self.get_json_value_terms(expression_context)
        arguments: list[Any] = []
        for key, value_term in zip(self.keys, value_terms, strict=True):
            arguments += [ValueWrapper(key, allow_parametrize=False), value_term]
        self.field_object = self.OUTPUT_FIELD  # type: ignore[call-overload]
        return ExpressionResult(term=JsonObjectTerm(*arguments), joins=joins, output_field=self.OUTPUT_FIELD)  # type: ignore[call-overload]

    @classmethod
    def get_typed_literal_term(
        cls, term: Any, value_type: JsonValueType, expression_context: ExpressionContext
    ) -> Any:
        """A literal value typed where the dialect says nothing around it types the parameter."""
        if not isinstance(term, ValueWrapper):
            return term
        cast_sql_type = expression_context.dialect.parameters.get_json_object_value_cast_type(
            Value.get_bound_value(term.value), value_type
        )
        return term if cast_sql_type is None else CastTerm(term, cast_sql_type)

    value_field = OUTPUT_FIELD


__all__ = ["JSONObject"]
