from __future__ import annotations

from decimal import Decimal

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
