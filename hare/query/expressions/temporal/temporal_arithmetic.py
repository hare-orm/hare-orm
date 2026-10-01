from __future__ import annotations

import functools
import operator
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import FieldError
from hare.fields.base.field import Field
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.generated import GeneratedField
from hare.query.expressions.constants import TEMPORAL_FIELD_TYPES, TEMPORAL_LITERAL_TYPES, TEMPORAL_RESULT_TYPES
from hare.query.expressions.enums import ArithmeticOperator, TemporalType
from hare.sql.functions.temporal_difference import TemporalDifference
from hare.sql.functions.temporal_shift import TemporalShift
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
from hare.query.expressions.temporal.temporal_operand import TemporalOperand


class TemporalArithmetic:
    """Rules and SQL for ``+``/``-`` between date, datetime and timedelta operands: ``datetime +/-
    timedelta``, ``date +/- timedelta`` (the sub-day remainder is dropped), ``datetime - datetime``,
    ``date - date`` and ``timedelta +/- timedelta``. Durations are absolute time, never calendar
    days. Any other combination with a date/time operand raises ``FieldError``.
    """

    #: Shared, long-lived instances - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    DATETIME_OUTPUT_FIELD = DatetimeField()
    DATE_OUTPUT_FIELD = DateField()
    TIME_OUTPUT_FIELD = TimeField()
    TIMEDELTA_OUTPUT_FIELD = TimeDeltaField()

    SHARED_OUTPUT_FIELDS: ClassVar[dict[TemporalType, Field[Any]]] = {
        TemporalType.DATETIME: DATETIME_OUTPUT_FIELD,
        TemporalType.DATE: DATE_OUTPUT_FIELD,
        TemporalType.TIMEDELTA: TIMEDELTA_OUTPUT_FIELD,
    }

    @staticmethod
    def get_field_encoder(field: Field[Any], dialect: Dialect) -> Callable[[Any], Any]:
        """The encoder binding a literal the way a column of ``field`` stores it.

        Args:
            field: The field.
            dialect: The dialect of the database the query runs on.

        Returns:
            A ``(value) -> value`` encoder.
        """
        return functools.partial(dialect.types.get_db_value, field, instance=None)

    @classmethod
    def get_literal_encoder(cls, temporal_type: TemporalType, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The encoder that turns a literal of ``temporal_type`` into its stored form.

        Args:
            temporal_type: The literal's temporal type.
            dialect: The dialect of the database the query runs on.

        Returns:
            The encoder, or None for a type that has no arithmetic (time).
        """
        output_field = cls.SHARED_OUTPUT_FIELDS.get(temporal_type)
        return None if output_field is None else cls.get_field_encoder(output_field, dialect)

    @staticmethod
    def get_literal_temporal_type(value: Any) -> TemporalType | None:
        """The temporal type of a Python literal.

        Args:
            value: The literal.

        Returns:
            Its type, or None when it isn't a datetime/date/time/timedelta.
        """
        for literal_type, temporal_type in TEMPORAL_LITERAL_TYPES:
            if isinstance(value, literal_type):
                return temporal_type
        return None

    @staticmethod
    def get_field_temporal_type(field: Field[Any] | None) -> TemporalType | None:
        """The temporal type of the values a field holds.

        Args:
            field: A field, possibly a GeneratedField wrapping the real one.

        Returns:
            Its type, or None when the field isn't a datetime/date/time/timedelta field.
        """
        if isinstance(field, GeneratedField):
            field = field.output_field
        for field_class, temporal_type in TEMPORAL_FIELD_TYPES:
            if isinstance(field, field_class):
                return temporal_type
        return None

    @staticmethod
    def is_temporal(left: TemporalOperand, right: TemporalOperand) -> bool:
        """Whether an expression follows the temporal rules. A timedelta field with a plain number
        stays microsecond arithmetic (``F("duration") * 2``); a timedelta literal needs a date/time
        operand.

        Args:
            left: The left operand.
            right: The right operand.

        Returns:
            True when ``build_term()`` builds the expression.
        """
        temporal_types = {left.type, right.type}
        if temporal_types & {TemporalType.DATETIME, TemporalType.DATE, TemporalType.TIME}:
            return True
        if left.type == right.type == TemporalType.TIMEDELTA:
            return True
        return any(operand.is_literal and operand.type == TemporalType.TIMEDELTA for operand in (left, right))

    @classmethod
    def build_term(
        cls, connector: ArithmeticOperator, left: TemporalOperand, right: TemporalOperand
    ) -> tuple[Term, Field[Any]]:
        """Builds the SQL term and result field of a temporal arithmetic expression.

        Args:
            connector: The arithmetic operator.
            left: The left operand.
            right: The right operand.

        Returns:
            The result term and the field its value is decoded through.

        Raises:
            FieldError: The operator/operand-type combination isn't supported.
        """
        if left.type is None or right.type is None:
            result_type = None
        else:
            result_type = TEMPORAL_RESULT_TYPES.get((connector, left.type, right.type))
        if result_type is None:
            raise FieldError(
                f"Unsupported arithmetic: {cls.describe(left)} {cls.get_operator_symbol(connector)} "
                f"{cls.describe(right)}. Supported: datetime +/- timedelta, date +/- timedelta, "
                "datetime - datetime, date - date, timedelta +/- timedelta - "
                "use a timedelta, not a number, to shift a date/time."
            )

        if left.type == right.type == TemporalType.TIMEDELTA:
            term = getattr(operator, connector)(left.term, right.term)
        elif result_type == TemporalType.TIMEDELTA:
            term = TemporalDifference(left.term, right.term, is_date=left.type == TemporalType.DATE)
        else:
            base, delta = (left, right) if right.type == TemporalType.TIMEDELTA else (right, left)
            term = TemporalShift(
                base.term,
                delta.term,
                is_date=base.type == TemporalType.DATE,
                is_subtraction=connector == ArithmeticOperator.SUB,
            )

        output_field = next(
            (operand.field for operand in (right, left) if operand.type == result_type and operand.field is not None),
            cls.SHARED_OUTPUT_FIELDS[result_type],
        )
        return term, output_field

    @classmethod
    def raise_if_not_assignable(
        cls, field_name: str, target_field: Field[Any], result_field: Field[Any] | None
    ) -> None:
        """Rejects storing an arithmetic result of one date/time type in a column of another
        (a datetime shift into a date column, a difference into a datetime column).

        Args:
            field_name: The name of the field being updated.
            target_field: The field being updated.
            result_field: The field the expression's result is decoded through.

        Raises:
            FieldError: The result and the column hold different date/time types.
        """
        target_type = cls.get_field_temporal_type(target_field)
        if target_type is None:
            return
        result_type = cls.get_field_temporal_type(result_field)
        if result_type is not None and result_type != target_type:
            raise FieldError(
                f"Cannot update {field_name!r} ({target_type.value}) from an expression that evaluates to a "
                f"{result_type.value}"
            )

    @staticmethod
    def get_operator_symbol(connector: ArithmeticOperator) -> str:
        """The operator symbol of a connector, for error messages."""
        return {
            ArithmeticOperator.ADD: "+",
            ArithmeticOperator.SUB: "-",
            ArithmeticOperator.MUL: "*",
            ArithmeticOperator.DIV: "/",
            ArithmeticOperator.MOD: "%",
            ArithmeticOperator.POW: "**",
        }[connector]

    @staticmethod
    def describe(operand: TemporalOperand) -> str:
        """A short description of an operand's type, for error messages."""
        if operand.type is None:
            return "a non-date/time value"
        return f"a {operand.type.value} {'literal' if operand.is_literal else 'value'}"
