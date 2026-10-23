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

#: Arithmetic connectors of Decimals a dialect may work out itself
#: (``TermRenderers.get_decimal_operation_term()``).
DECIMAL_OPERATION_CONNECTORS: frozenset[ArithmeticOperator] = frozenset(
    {ArithmeticOperator.ADD, ArithmeticOperator.SUB, ArithmeticOperator.MUL, ArithmeticOperator.DIV}
)

#: Arithmetic connectors whose integer result can outgrow the operands' type - the operands of one a
#: query reads are widened where the database wraps the result around; a written one is left to it.
EXACT_INTEGER_CONNECTORS: frozenset[ArithmeticOperator] = frozenset(
    {ArithmeticOperator.ADD, ArithmeticOperator.SUB, ArithmeticOperator.MUL}
)

#: The integer types a read result of arithmetic is checked against, by their range - wider ones can't be
#: widened to see the exact result.
CHECKED_INTEGER_RANGE = (-(2**127), 2**128 - 1)
#: What the out of range message calls an integer result.
INTEGER_TYPE_NAME = "integer"

#: How many operations an arithmetic expression nests at most - a deeper one is past what a query is
#: built from (Python's stack) and what a database parses.
MAX_ARITHMETIC_DEPTH = 1000
