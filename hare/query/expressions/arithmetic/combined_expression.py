from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, ClassVar, cast

from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.arithmetic.constants import (
    DECIMAL_MAX_SCALE_CONNECTORS,
    MAX_ARITHMETIC_DEPTH,
)
from hare.query.expressions.compatibility.constants import ARITHMETIC_REFUSED_VALUE_TYPES, JSON_ARITHMETIC_MESSAGE
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import LITERAL_CAST_SQL_TYPE
from hare.query.expressions.enums import ArithmeticOperator, NumericValueType, TemporalType
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.temporal.constants import DURATION_TEMPORAL_TYPES
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.temporal.temporal_operand import TemporalOperand
from hare.query.expressions.value import Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.functions.cast import Cast
from hare.sql.terms.functions.decimal_mod import DecimalMod
from hare.sql.terms.functions.float_mod import FloatMod
from hare.sql.terms.functions.mod import Mod
from hare.sql.terms.term import Term


class CombinedExpression(CombinableExpression):
    """Two expressions joined by an arithmetic operator - ``F('price') * 2``."""

    #: How many operations the expression nests - one, unless an operand is an operation too.
    depth: int = 1

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("connector", PlanPartType.KEY),
        ("left", PlanPartType.EXPRESSION),
        ("right", PlanPartType.EXPRESSION),
        # Counted from the operands, which are in the key.
        ("depth", PlanPartType.NONE),
    )

    def __init__(self, left: Expression, connector: ArithmeticOperator, right: Any) -> None:
        self.left = left
        self.connector = connector
        self.right = self.get_operand_expression(right)
        if type(left) is CombinedExpression or type(self.right) is CombinedExpression:
            self.set_depth()

    def set_depth(self) -> None:
        """Sets how many operations this one nests, over operands that nest some.

        Raises:
            QueryError: It nests more than ``MAX_ARITHMETIC_DEPTH`` - every part of a query reads it
                level by level, past Python's stack.
        """
        left, right = self.left, self.right
        self.depth = 1 + max(
            left.depth if type(left) is CombinedExpression else 0,
            right.depth if type(right) is CombinedExpression else 0,
        )
        if self.depth > MAX_ARITHMETIC_DEPTH:
            raise QueryError(
                f"An arithmetic expression nests more than {MAX_ARITHMETIC_DEPTH} operations - no database "
                "builds it; write a long sum or chain as a few shorter ones"
            )

    # Lets QueryAnnotations.get_annotate() decode this annotation's result through get_result()'s own
    # output_field, which is None whenever an operand shifts the result away from a known type
    # (e.g. a float literal against an IntField) - the raw driver value is kept then.
    populate_field_object = True

    @staticmethod
    def _literal_term(operand: Expression, term: Term) -> Term:
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

    def _get_scaled_duration_field(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> Field[Any] | None:
        """The field of a duration multiplied or divided by a number - a duration, as in
        Python: ``F("duration") * 2``, ``2 * F("duration")``, ``F("duration") / F("count")``.

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Returns:
            The duration operand's field; None for any other arithmetic.
        """
        candidates: tuple[tuple[Field[Any] | None, Expression, Field[Any] | None], ...]
        if self.connector is ArithmeticOperator.MUL:
            candidates = (
                (left_output_field, self.right, right_output_field),
                (right_output_field, self.left, left_output_field),
            )
        elif self.connector is ArithmeticOperator.DIV:
            candidates = ((left_output_field, self.right, right_output_field),)
        else:
            return None
        for duration_field, number_operand, number_field in candidates:
            if (
                TemporalArithmetic.get_field_temporal_type(duration_field) in DURATION_TEMPORAL_TYPES
                and TemporalArithmetic.get_field_temporal_type(number_field) is None
                and self._get_operand_numeric_type(number_operand, number_field) is not None
            ):
                return duration_field
        return None

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
                # Precision over dialect parity: a database with an exact numeric keeps its quotient,
                # one dividing in doubles returns the nearest Decimal of its float result.
                return NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD
            return NumericTyping.get_decimal_output_field(self._get_decimal_result_scale(decimal_scales))
        has_literal = left_output_field is None or right_output_field is None
        if any(
            not isinstance(GeneratedField.get_effective_field(field), IntField) or NumericTyping.is_enum_field(field)
            for field in fields
        ):
            if not has_literal and left_output_field is right_output_field:
                return left_output_field
            # Microseconds of a duration, or the integer value of an enum member.
            return NumericTyping.INTEGER_OUTPUT_FIELD
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
    def _are_plain_integer_fields(left_output_field: Field[Any], right_output_field: Field[Any]) -> bool:
        """Whether both operand fields are integer fields of any width, neither of them an enum.

        Args:
            left_output_field: The left operand's field.
            right_output_field: The right operand's field.

        Returns:
            True when integer arithmetic between the two fields is well-defined.
        """
        return all(
            isinstance(GeneratedField.get_effective_field(field), IntField) and not NumericTyping.is_enum_field(field)
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
                isinstance(GeneratedField.get_effective_field(integer_field), IntField)
                and not NumericTyping.is_enum_field(integer_field)
                and isinstance(GeneratedField.get_effective_field(fraction_field), (FloatField, DecimalField))
            ):
                return True
        return False

    @staticmethod
    def _raise_if_not_a_number(operand: Expression, field: Field[Any] | None) -> None:
        """Rejects arithmetic on text or a boolean - hare's arithmetic is of numbers, and a database
        would compute a number of it or refuse it.

        Args:
            operand: The operand, of no date/time type.
            field: The field its value is decoded through, when it has one.

        Raises:
            FieldError: The operand is text, a boolean or a JSON value - most databases have no
                arithmetic of JSON, another would compute one of the value it reads.
        """
        if field is not None and isinstance(GeneratedField.get_effective_field(field), JSONField):
            raise FieldError(JSON_ARITHMETIC_MESSAGE)
        branch = (
            ResultBranch(operand.value, None, is_literal=True)
            if isinstance(operand, Value)
            else ResultBranch(None, field, is_literal=False)
        )
        value_type = OutputFieldCompatibility.get_value_type(branch)
        if value_type in ARITHMETIC_REFUSED_VALUE_TYPES:
            raise FieldError(f"Cannot use arithmetic on {value_type} - convert it to a number with Cast()")

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
            raise FieldError(
                "Cannot use arithmetic between fields of different types "
                f"({type(left_output_field).__name__}, {type(right_output_field).__name__})"
            )

    def raise_if_duration_arithmetic_refused(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> None:
        """Refuses arithmetic a duration has no result of - a duration (a ``TimeDeltaField``'s
        microseconds, an interval) is added to or taken from a duration, multiplied by a number and
        divided by one; a number added to one would expose the microseconds as a number, the rest a
        database computes of the microseconds or refuses.

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Raises:
            FieldError: A duration is in any other arithmetic.
        """
        is_left_duration = self.is_duration_field(left_output_field)
        is_right_duration = self.is_duration_field(right_output_field)
        if not (is_left_duration or is_right_duration):
            return
        is_left_number = self.is_number_operand(self.left, left_output_field)
        is_right_number = self.is_number_operand(self.right, right_output_field)
        if self.connector in (ArithmeticOperator.ADD, ArithmeticOperator.SUB):
            if is_left_number or is_right_number:
                raise FieldError("Cannot add a number to a duration or take one from it - give a timedelta")
            return
        if self.connector is ArithmeticOperator.MUL and (
            (is_left_duration and is_right_number) or (is_right_duration and is_left_number)
        ):
            return
        if self.connector is ArithmeticOperator.DIV and is_left_duration and is_right_number:
            return
        raise FieldError(
            f"Cannot use {TemporalArithmetic.get_operator_symbol(self.connector)} with a duration here - a "
            "duration is added to or taken from a duration, multiplied by a number and divided by one"
        )

    @staticmethod
    def is_duration_field(field: Field[Any] | None) -> bool:
        """Whether a field holds durations - a ``TimeDeltaField``, or a dialect's interval.

        Args:
            field: The field, None for a value of no field.

        Returns:
            True for a duration field.
        """
        if field is None:
            return False
        effective_field = GeneratedField.get_effective_field(field)
        return isinstance(effective_field, TimeDeltaField) or effective_field.holds_intervals

    @staticmethod
    def is_number_operand(operand: Expression, field: Field[Any] | None) -> bool:
        """Whether an operand is a number - a numeric literal, or a value of a numeric field.

        Args:
            operand: The operand.
            field: The field its value is decoded through, if it has one.

        Returns:
            True for a number.
        """
        if isinstance(operand, Value):
            return isinstance(operand.value, (int, float, Decimal)) and not isinstance(operand.value, bool)
        return NumericTyping.get_field_type(field) is not None

    def raise_if_text_literal_beside_number(
        self, left_output_field: Field[Any] | None, right_output_field: Field[Any] | None
    ) -> None:
        """Refuses a text literal combined with a numeric column - a number is given as a number, as
        a filter of the column takes it (the value hidden for a sensitive column).

        Args:
            left_output_field: The left operand's field, if it has one.
            right_output_field: The right operand's field, if it has one.

        Raises:
            ValidationError: A text literal stands beside a numeric column.
        """
        for literal_side, column_field in (("left", right_output_field), ("right", left_output_field)):
            operand = getattr(self, literal_side)
            if not isinstance(operand, Value) or not isinstance(operand.value, str):
                continue
            if column_field is None or NumericTyping.get_field_type(column_field) is None:
                continue
            raise column_field.get_validation_error(
                TypeError(operand.value),
                operand.value,
                f"{column_field.model_field_name}: {column_field.get_value_for_message(operand.value)} is a text, "
                "not a number - give the number itself",
            )

    @staticmethod
    def _get_operand(
        operand: Expression, result: ExpressionResult, interval_field: Field[Any] | None
    ) -> TemporalOperand:
        if isinstance(operand, Value):
            temporal_type = TemporalArithmetic.get_literal_temporal_type(operand.value)
            if temporal_type is TemporalType.TIMEDELTA and interval_field is not None:
                # A duration beside an interval column is an interval, bound as the column binds one.
                return TemporalOperand(
                    term=result.term, type=TemporalType.INTERVAL, field=interval_field, is_literal=True
                )
            return TemporalOperand(term=result.term, type=temporal_type, field=None, is_literal=True)
        field = operand.get_value_field(result)
        return TemporalOperand(
            term=result.term, type=TemporalArithmetic.get_field_temporal_type(field), field=field, is_literal=False
        )

    @staticmethod
    def _get_literal_result(
        literal: Value, interval_field: Field[Any] | None, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Resolves a literal operand; a date/time one is converted to its stored form first - a timedelta
        beside an interval column to the column's.

        Args:
            literal: The literal.
            interval_field: The interval field of the other operand, if it is one.
            expression_context: The context being resolved.

        Returns:
            The resolved literal.
        """
        temporal_type = TemporalArithmetic.get_literal_temporal_type(literal.value)
        if temporal_type is None:
            return literal.get_result(expression_context)
        if temporal_type is TemporalType.TIMEDELTA and interval_field is not None:
            encoder: Callable[[Any], Any] | None = TemporalArithmetic.get_field_encoder(
                interval_field, expression_context.dialect
            )
        else:
            encoder = TemporalArithmetic.get_literal_encoder(temporal_type, expression_context.dialect)
        return literal.get_encoded_result(expression_context, encoder)

    @staticmethod
    def get_interval_field(operand: Expression, result: ExpressionResult | None) -> Field[Any] | None:
        """The field of an operand holding a dialect's interval.

        Args:
            operand: The operand.
            result: Its resolved result, None for a literal.

        Returns:
            The interval field, None for any other operand.
        """
        if result is None:
            return None
        field = operand.get_value_field(result)
        return field if TemporalArithmetic.get_field_temporal_type(field) is TemporalType.INTERVAL else None

    def _get_arithmetic_term(
        self,
        left_term: Term,
        right_term: Term,
        numeric_types: list[NumericType] | None,
        expression_context: ExpressionContext,
        operand_fields: tuple[Field[Any] | None, Field[Any] | None],
    ) -> Term:
        """Builds the SQL of the arithmetic.

        Args:
            left_term: The left operand's term.
            right_term: The right operand's term.
            numeric_types: Both operands' numeric types, None when not both known numbers.
            expression_context: The context being resolved.
            operand_fields: Each operand's field, if it has one.

        Returns:
            The arithmetic term - an exact remainder for integers and Decimals, a floating-point
            one when a float is involved, and a floating-point quotient for Decimals where the
            database divides in doubles.
        """
        value_types = {numeric_type.type for numeric_type in numeric_types} if numeric_types is not None else set()
        if numeric_types is not None and value_types == {NumericValueType.INTEGER, NumericValueType.FLOAT}:
            # Some databases have no arithmetic of a wide integer and a float - the dialect converts the
            # integer operand.
            renderers = expression_context.dialect.renderers
            if numeric_types[0].type is NumericValueType.INTEGER:
                left_term = renderers.get_float_operand(left_term, operand_fields[0])
            else:
                right_term = renderers.get_float_operand(right_term, operand_fields[1])
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
        # Term's own SQL arithmetic - an operand term (RawSQL, Subquery) overrides the operators to
        # build a CombinedExpression instead.
        return getattr(Term, f"__{self.connector}__")(left_term, right_term)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        left = None if isinstance(self.left, Value) else self.left.get_result(expression_context)
        right = None if isinstance(self.right, Value) else self.right.get_result(expression_context)
        left_interval_field = self.get_interval_field(self.left, left)
        right_interval_field = self.get_interval_field(self.right, right)
        if left is None:
            left = self._get_literal_result(cast("Value", self.left), right_interval_field, expression_context)
        if right is None:
            right = self._get_literal_result(cast("Value", self.right), left_interval_field, expression_context)
        left_operand = self._get_operand(self.left, left, right_interval_field)
        right_operand = self._get_operand(self.right, right, left_interval_field)
        for operand in (left_operand, right_operand):
            EncryptedFieldBase.raise_if_encrypted(operand.field, "an arithmetic expression")
        left_output_field, right_output_field = left_operand.field, right_operand.field
        if TemporalArithmetic.is_temporal(left_operand, right_operand):
            term, temporal_output_field = TemporalArithmetic.build_term(self.connector, left_operand, right_operand)
            return ExpressionResult(
                term=term,
                joins=ExpressionResult.dedup_joins(left.joins, right.joins),
                output_field=temporal_output_field,
            )
        self.raise_if_text_literal_beside_number(left_output_field, right_output_field)
        self.raise_if_duration_arithmetic_refused(left_output_field, right_output_field)
        scaled_duration_field = self._get_scaled_duration_field(left_output_field, right_output_field)
        if scaled_duration_field is None:
            self._raise_if_different_field_types(left_output_field, right_output_field)
        self._raise_if_not_a_number(self.left, left_output_field)
        self._raise_if_not_a_number(self.right, right_output_field)
        left_term = self._literal_term(self.left, left.term)
        right_term = self._literal_term(self.right, right.term)
        if NumericTyping.is_enum_field(left_output_field):
            left_term = expression_context.dialect.renderers.get_enum_number_term(left_term)
        if NumericTyping.is_enum_field(right_output_field):
            right_term = expression_context.dialect.renderers.get_enum_number_term(right_term)
        numeric_types = self._get_numeric_types(left_output_field, right_output_field)
        output_field = scaled_duration_field or self._get_output_field(left_output_field, right_output_field)
        if (
            scaled_duration_field is None
            and self.connector is ArithmeticOperator.DIV
            and numeric_types is not None
            and {numeric_type.type for numeric_type in numeric_types} == {NumericValueType.INTEGER}
            and expression_context.dialect.features.divides_integers_exactly
        ):
            # The database's own quotient of integers - a fraction.
            output_field = NumericTyping.FLOAT_OUTPUT_FIELD
        if (
            scaled_duration_field is not None
            and self.connector is ArithmeticOperator.DIV
            and numeric_types is not None
            and {numeric_type.type for numeric_type in numeric_types} == {NumericValueType.INTEGER}
        ):
            renderers = expression_context.dialect.renderers
            term = renderers.get_duration_quotient(left_term, right_term)
        else:
            term = self._get_arithmetic_term(
                left_term, right_term, numeric_types, expression_context, (left_output_field, right_output_field)
            )
        if TemporalArithmetic.get_field_temporal_type(scaled_duration_field) is TemporalType.TIMEDELTA:
            # A duration hare keeps as microseconds stays of its column's type.
            term = expression_context.dialect.renderers.get_scaled_duration(term)
        return ExpressionResult(
            term=term,
            joins=ExpressionResult.dedup_joins(left.joins, right.joins),
            output_field=output_field,
        )
