from __future__ import annotations

from decimal import Decimal

from hare.fields.data.constants import INT16_MAX, INT16_MIN, INT32_MAX, INT32_MIN, INT64_MAX, INT64_MIN
from hare.query.expressions.enums import ArithmeticOperator, NumericValueType

#: Python type a text literal is converted to before it is combined, in arithmetic, with a column
#: holding numbers of each type.
NUMERIC_VALUE_TYPE_CONVERTERS: dict[NumericValueType, type] = {
    NumericValueType.INTEGER: int,
    NumericValueType.FLOAT: float,
    NumericValueType.DECIMAL: Decimal,
}

#: Arithmetic connectors whose Decimal result scale is the larger of the operands' scales -
#: `mul` adds them instead.
DECIMAL_MAX_SCALE_CONNECTORS: frozenset[ArithmeticOperator] = frozenset(
    {
        ArithmeticOperator.ADD,
        ArithmeticOperator.SUB,
        ArithmeticOperator.DIV,
        ArithmeticOperator.MOD,
        ArithmeticOperator.POW,
    }
)

#: Arithmetic connectors dividing by their right operand - a zero divisor raises.
DIVISION_CONNECTORS: frozenset[ArithmeticOperator] = frozenset({ArithmeticOperator.DIV, ArithmeticOperator.MOD})

#: Arithmetic connectors whose integer result can outgrow the operands' type - the operands of one
#: written into a column are widened where the database wraps the result around.
EXACT_INTEGER_CONNECTORS: frozenset[ArithmeticOperator] = frozenset(
    {ArithmeticOperator.ADD, ArithmeticOperator.SUB, ArithmeticOperator.MUL}
)

#: The integer types a read result of arithmetic is checked against, by their range - wider ones can't be
#: widened to see the exact result.
CHECKED_INTEGER_RANGE = (-(2**127), 2**128 - 1)
#: PostgreSQL's names of the integer types, by their range - the out of range message names the type.
INTEGER_TYPE_NAMES = {
    (INT16_MIN, INT16_MAX): "smallint",
    (INT32_MIN, INT32_MAX): "integer",
    (INT64_MIN, INT64_MAX): "bigint",
}
#: The name of any other integer type in the message.
DEFAULT_INTEGER_TYPE_NAME = "integer"
