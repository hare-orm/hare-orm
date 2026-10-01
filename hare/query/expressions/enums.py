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


class ValueRefOrigin(StrEnum):
    """Where a recorded non-filter value of a statement plan comes from."""

    ANNOTATION = "annotation"
    CURSOR = "cursor"
    SUBQUERY = "subquery"
