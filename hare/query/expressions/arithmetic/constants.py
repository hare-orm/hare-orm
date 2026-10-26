from __future__ import annotations

from hare.query.expressions.enums import ArithmeticOperator

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

#: How many operations an arithmetic expression nests at most - a deeper one is past what a query is
#: built from (Python's stack) and what a database parses.
MAX_ARITHMETIC_DEPTH = 1000
