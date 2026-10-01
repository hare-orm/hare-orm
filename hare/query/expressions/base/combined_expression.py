from __future__ import annotations

from copy import copy
from typing import Any

from hare.exceptions import FieldError
from hare.fields.base.field import Field
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.query.expressions.base.arithmetic_expression_mixin import ArithmeticExpressionMixin
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.expressions.constants import (
    DECIMAL_MAX_SCALE_CONNECTORS,
    LITERAL_CAST_SQL_TYPE,
    NUMERIC_VALUE_TYPE_CONVERTERS,
)
from hare.query.expressions.enums import ArithmeticOperator, NumericValueType
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.temporal.temporal_operand import TemporalOperand
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.functions.cast import Cast
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.decimal_mod import DecimalMod
from hare.sql.terms.functions.declarations import FloatMod
from hare.sql.terms.functions.mod import Mod


class CombinedExpression(ArithmeticExpressionMixin):
    def __init__(self, left: Expression, connector: ArithmeticOperator, right: Any) -> None:
        self.left = left
        self.connector = connector
        self.right = self.get_operand_expression(right)

    # Lets QuerySet._get_annotate() decode this annotation's result through get_result()'s own
    # output_field, which is None whenever an operand shifts the result away from a known type
    # (e.g. a float literal against an IntField) - the raw driver value is kept then.
    populate_field_object = True

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The operator and both operands' descriptions - a literal operand's type decides its
        cast.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None when an operand keeps no plan.
        """
        return PlanDescription.combine(
            (type(self), self.connector),
            (self.left.get_plan_description(context), self.right.get_plan_description(context)),
        )

    def _literal_term(self, operand: Expression, term: Term) -> Term:
        if not isinstance(operand, Value):
            return term
        cast_sql_type = Value.get_cast_sql_type(operand.value, LITERAL_CAST_SQL_TYPE)
        if cast_sql_type is None:
            return term
        return Cast(term, cast_sql_type)

    @staticmethod
    def _get_operand_numeric_type(operand: Expression, output_field: Field[Any] | None) -> NumericType | None:
        """The type of number an operand is - a TimeDeltaField counts as its whole microseconds.

        Args:
            operand: The operand.
            output_field: The operand's field, if it has one.

        Returns:
            The numeric type, or None when the operand isn't a known number.
        """
        if output_field is not None:
            return NumericTyping.get_field_type(output_field, timedelta_as_integer=True)
        if isinstance(operand, Value):
            return NumericTyping.get_literal_type(operand.value)
        return None

    def _get_numeric_types(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> list[NumericType] | None:
        """Both operands' numeric types.

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Returns:
            The two types, or None when either operand isn't a known number.
        """
        numeric_types = [
            self._get_operand_numeric_type(self.left, left_output_field),
            self._get_operand_numeric_type(self.right, right_output_field),
        ]
        known_numeric_types = [numeric_type for numeric_type in numeric_types if numeric_type is not None]
        if len(known_numeric_types) != len(numeric_types):
            return None
        return known_numeric_types

    def _get_output_field(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> Field[Any] | None:
        """The field the (non-temporal) result is decoded through.

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Returns:
            A float field when a float is involved, else a Decimal field when a Decimal is, else
            the integer field; for non-numbers the operands' field. None (the raw driver value)
            when an operand's type is unknown.
        """
        operands = ((self.left, left_output_field), (self.right, right_output_field))
        if left_output_field is None and right_output_field is None:
            return None
        if any(
            operand_output_field is None and not isinstance(operand, Value)
            for operand, operand_output_field in operands
        ):
            return None
        numeric_types = self._get_numeric_types(left_output_field, right_output_field)
        if numeric_types is not None:
            return self._get_numeric_output_field(numeric_types, left_output_field, right_output_field)
        for operand, operand_output_field in operands:
            if operand_output_field is None and isinstance(operand, Value) and operand.value is not None:
                # A literal changes the type of a non-numeric field's value.
                return None
        return right_output_field or left_output_field

    def _get_numeric_output_field(
        self,
        numeric_types: list[NumericType],
        left_output_field: Field[Any] | None,
        right_output_field: Field[Any] | None,
    ) -> Field[Any] | None:
        """The field a result computed from two numbers is decoded through.

        Args:
            numeric_types: Both operands' numeric types.
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Returns:
            The result field.
        """
        fields = [field for field in (right_output_field, left_output_field) if field is not None]
        value_types = {numeric_type.type for numeric_type in numeric_types}
        if NumericValueType.FLOAT in value_types:
            return NumericTyping.get_common_output_field(numeric_types, fields)
        if NumericValueType.DECIMAL in value_types:
            decimal_scales = NumericTyping.get_decimal_scales(numeric_types)
            if self.connector is ArithmeticOperator.DIV or None in decimal_scales:
                # Precision over dialect parity: Postgres keeps its exact numeric quotient,
                # SQLite (double arithmetic) returns the nearest Decimal of its float result.
                return NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type]
            return NumericTyping.get_decimal_output_field(self._get_decimal_result_scale(decimal_scales))
        has_literal = left_output_field is None or right_output_field is None
        if any(
            not isinstance(NumericTyping.get_effective_field(field), IntField) or NumericTyping.is_enum_field(field)
            for field in fields
        ):
            if not has_literal and left_output_field is right_output_field:
                return left_output_field
            # Microseconds of a duration, or the integer value of an enum member.
            return NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
        return right_output_field or left_output_field

    def _get_decimal_result_scale(self, decimal_scales: list[int | None]) -> int:
        """Scale of a Decimal arithmetic result - the operands' summed scales for `*`, the
        larger one for every other connector.

        Args:
            decimal_scales: Both operands' scales, all known.

        Returns:
            The result's scale.
        """
        known_scales = [scale for scale in decimal_scales if scale is not None]
        if self.connector in DECIMAL_MAX_SCALE_CONNECTORS:
            return max(known_scales)
        return sum(known_scales)

    @staticmethod
    def _get_effective_field(field: Field[Any]) -> Field[Any]:
        """Unwraps a GeneratedField to the field its value actually has."""
        return NumericTyping.get_effective_field(field)

    @staticmethod
    def _are_plain_integer_fields(left_output_field: Field[Any], right_output_field: Field[Any]) -> bool:
        """Whether both operand fields are integer fields of any width, neither of them an enum.

        Args:
            left_output_field: The left operand's field.
            right_output_field: The right operand's field.

        Returns:
            True when integer arithmetic between the two fields is well-defined.
        """
        return all(
            isinstance(NumericTyping.get_effective_field(field), IntField) and not NumericTyping.is_enum_field(field)
            for field in (left_output_field, right_output_field)
        )

    @staticmethod
    def _is_integer_with_fraction_field(left_output_field: Field[Any], right_output_field: Field[Any]) -> bool:
        """Whether one operand field is a plain integer field and the other a float or Decimal
        field - Django's mixed numeric combinations, whose result has the fraction field's type.

        Args:
            left_output_field: The left operand's field.
            right_output_field: The right operand's field.

        Returns:
            True for an integer field combined with a float or Decimal field.
        """
        for integer_field, fraction_field in (
            (left_output_field, right_output_field),
            (right_output_field, left_output_field),
        ):
            if (
                isinstance(NumericTyping.get_effective_field(integer_field), IntField)
                and not NumericTyping.is_enum_field(integer_field)
                and isinstance(NumericTyping.get_effective_field(fraction_field), (FloatField, DecimalField))
            ):
                return True
        return False

    @staticmethod
    def _raise_if_different_field_types(
        left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> None:
        """Rejects arithmetic between two model columns of different types - a computed value (a
        count, a length, an arithmetic or literal result) combines with any number.

        Args:
            left_output_field: The left operand's field.
            right_output_field: The right operand's field.

        Raises:
            FieldError: Both operands are model columns of different types other than two integer
                fields or an integer field with a float or Decimal field.
        """
        if (
            left_output_field is not None
            and right_output_field is not None
            and type(left_output_field) is not type(right_output_field)
            and not CombinedExpression._are_plain_integer_fields(left_output_field, right_output_field)
            and not CombinedExpression._is_integer_with_fraction_field(left_output_field, right_output_field)
            and NumericTyping.is_model_field(left_output_field)
            and NumericTyping.is_model_field(right_output_field)
        ):
            raise FieldError("Cannot use arithmetic expression between different field type")

    def get_with_text_literal_converted(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> CombinedExpression | None:
        """Converts a text literal combined with a numeric column to that column's type of number,
        so a malformed one fails as the column's ``ValidationError`` (hiding the value for a
        sensitive column) instead of reaching the driver as is.

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Returns:
            A copy with the literal converted, or None when there is nothing to convert.

        Raises:
            ValidationError: The literal isn't a number.
        """
        for literal_side, column_field in (("left", right_output_field), ("right", left_output_field)):
            operand = getattr(self, literal_side)
            if not isinstance(operand, Value) or not isinstance(operand.value, str):
                continue
            numeric_type = NumericTyping.get_field_type(column_field)
            if column_field is None or numeric_type is None:
                continue
            validation_error = None
            try:
                converted_value = NUMERIC_VALUE_TYPE_CONVERTERS[numeric_type.type](operand.value)
            except (ValueError, ArithmeticError) as exc:
                validation_error = column_field.get_validation_error(
                    exc,
                    operand.value,
                    f"{column_field.model_field_name}: {column_field.get_value_for_message(operand.value)} "
                    "is not a number",
                )
            if validation_error is not None:
                raise validation_error
            converted_expression = copy(self)
            setattr(converted_expression, literal_side, Value(converted_value))
            return converted_expression
        return None

    @staticmethod
    def _get_operand(operand: Expression, result: ExpressionResult) -> TemporalOperand:
        if isinstance(operand, Value):
            temporal_type = TemporalArithmetic.get_literal_temporal_type(operand.value)
            return TemporalOperand(term=result.term, type=temporal_type, field=None, is_literal=True)
        field = operand.get_value_field(result)
        return TemporalOperand(
            term=result.term, type=TemporalArithmetic.get_field_temporal_type(field), field=field, is_literal=False
        )

    @staticmethod
    def _get_operand_result(operand: Expression, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves an operand; a date/time literal is converted to its stored form first."""
        if (
            isinstance(operand, Value)
            and (temporal_type := TemporalArithmetic.get_literal_temporal_type(operand.value)) is not None
        ):
            return operand.get_encoded_result(
                expression_context,
                TemporalArithmetic.get_literal_encoder(temporal_type, expression_context.dialect),
            )
        return operand.get_result(expression_context)

    def _get_arithmetic_term(
        self,
        left_term: Term,
        right_term: Term,
        numeric_types: list[NumericType] | None,
        expression_context: ExpressionContext,
    ) -> Term:
        """Builds the SQL of the arithmetic.

        Args:
            left_term: The left operand's term.
            right_term: The right operand's term.
            numeric_types: Both operands' numeric types, None when not both known numbers.
            expression_context: The context being resolved.

        Returns:
            The arithmetic term - an exact remainder for integers and Decimals, a floating-point
            one when a float is involved, and on SQLite a floating-point quotient for Decimals.
        """
        value_types = {numeric_type.type for numeric_type in numeric_types} if numeric_types is not None else set()
        if self.connector is ArithmeticOperator.MOD:
            if numeric_types is None:
                return Mod(left_term, right_term)
            if value_types == {NumericValueType.INTEGER}:
                return Mod(left_term, right_term, integer=True)
            if NumericValueType.FLOAT in value_types:
                return FloatMod(left_term, right_term)
            decimal_scales = NumericTyping.get_decimal_scales(numeric_types)
            known_scales = [scale for scale in decimal_scales if scale is not None]
            scale = max(known_scales) if len(known_scales) == len(decimal_scales) else None
            return DecimalMod(left_term, right_term, scale)
        if (
            self.connector is ArithmeticOperator.DIV
            and NumericValueType.DECIMAL in value_types
            and NumericValueType.FLOAT not in value_types
        ):
            left_term = expression_context.dialect.get_decimal_dividend(left_term)
        # Term's own SQL arithmetic - an operand term (RawSQL, Subquery) overrides the operators to
        # build a CombinedExpression instead.
        return getattr(Term, f"__{self.connector}__")(left_term, right_term)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        left = self._get_operand_result(self.left, expression_context)
        right = self._get_operand_result(self.right, expression_context)
        left_operand = self._get_operand(self.left, left)
        right_operand = self._get_operand(self.right, right)
        for operand in (left_operand, right_operand):
            EncryptedFieldMixin.raise_if_encrypted(operand.field, "an arithmetic expression")
        left_output_field, right_output_field = left_operand.field, right_operand.field
        if TemporalArithmetic.is_temporal(left_operand, right_operand):
            term, temporal_output_field = TemporalArithmetic.build_term(self.connector, left_operand, right_operand)
            return ExpressionResult(
                term=term,
                joins=ExpressionResult.dedup_joins(left.joins, right.joins),
                output_field=temporal_output_field,
            )
        converted_expression = self.get_with_text_literal_converted(left_output_field, right_output_field)
        if converted_expression is not None:
            return converted_expression.get_result(expression_context)
        self._raise_if_different_field_types(left_output_field, right_output_field)
        left_term = self._literal_term(self.left, left.term)
        right_term = self._literal_term(self.right, right.term)
        numeric_types = self._get_numeric_types(left_output_field, right_output_field)
        return ExpressionResult(
            term=self._get_arithmetic_term(left_term, right_term, numeric_types, expression_context),
            joins=ExpressionResult.dedup_joins(left.joins, right.joins),
            output_field=self._get_output_field(left_output_field, right_output_field),
        )
