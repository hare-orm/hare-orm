from __future__ import annotations

from enum import StrEnum


class ArithmeticOperator(StrEnum):
    """Arithmetic operator connecting two expressions - each value is the corresponding
    attribute name on the stdlib `operator` module (see TemporalArithmetic.build_term())."""

    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "truediv"
    POW = "pow"
    MOD = "mod"


class WindowFrameUnit(StrEnum):
    """What a window frame's offsets count - rows, or ordering values."""

    ROWS = "ROWS"
    RANGE = "RANGE"


class TemporalType(StrEnum):
    """Type of date/time value an arithmetic operand holds."""

    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"
    TIMEDELTA = "timedelta"


class NumericValueType(StrEnum):
    """Type of number an expression's value is."""

    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
