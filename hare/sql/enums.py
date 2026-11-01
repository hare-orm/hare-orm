from __future__ import annotations

from enum import StrEnum


class Arithmetic(StrEnum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"


class Comparator(StrEnum):
    pass


class Equality(Comparator):
    EQ = "="
    NE = "<>"
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


class Matching(Comparator):
    LIKE = " LIKE "
    REGEX = " REGEX "


class Boolean(Comparator):
    AND = "AND"
    OR = "OR"
    XOR = "XOR"


class JoinType(StrEnum):
    """How a source is joined - the dialect's clauses write the keyword of each."""

    INNER = "inner"
    LEFT_OUTER = "left_outer"
    #: Each row joined with the one row of the other table closest to it by an inequality, after
    #: the equalities - or with NULLs when none is.
    ASOF_LEFT = "asof_left"
    #: Each row repeated with each element of an array of it - no row for an empty array, or (left) a
    #: row with the element type's default.
    ARRAY = "array"
    LEFT_ARRAY = "left_array"


class SetOperation(StrEnum):
    UNION = "UNION"
    UNION_ALL = "UNION ALL"
    INTERSECT = "INTERSECT"
    EXCEPT_OF = "EXCEPT"


class DatePart(StrEnum):
    YEAR = "YEAR"
    ISO_YEAR = "ISOYEAR"
    QUARTER = "QUARTER"
    MONTH = "MONTH"
    WEEK = "WEEK"
    WEEK_DAY = "DOW"
    ISO_WEEK_DAY = "ISODOW"
    DAY = "DAY"
    HOUR = "HOUR"
    MINUTE = "MINUTE"
    SECOND = "SECOND"
    MICROSECOND = "MICROSECOND"


class CastType(StrEnum):
    """A type of value ``Cast()`` converts from or to."""

    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    UNKNOWN = "unknown"


class JsonValueType(StrEnum):
    """How a value is written into a JSON value built by a database - a type a database keeps in a
    form of its own."""

    PLAIN = "plain"
    DECIMAL = "decimal"
    DATETIME = "datetime"
    BINARY = "binary"
    JSON = "json"


class TruncType(StrEnum):
    """What ``Trunc()`` keeps of a date/time - a truncation unit, the part of hare's function where
    the dialect has one; ``date``/``time`` take a datetime's date or time of day."""

    YEAR = "year"
    QUARTER = "quarter"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"
    DATE = "date"
    TIME = "time"


class DateTruncSource(StrEnum):
    """The type of value ``Trunc()`` truncates and ``Extract()`` reads a part of."""

    DATETIME = "datetime"
    #: A date and a wall clock time without a time zone - read as it is, in no zone.
    NAIVE_DATETIME = "naive datetime"
    DATE = "date"
    TIME = "time"


class DatetimeCastTarget(StrEnum):
    """What a datetime is cast to - the SQL type name, the part of hare's function where the dialect
    has one."""

    DATE = "DATE"
    TIME = "TIME"


class AnalyticFunctionName(StrEnum):
    RANK = "RANK"
    DENSE_RANK = "DENSE_RANK"
    ROW_NUMBER = "ROW_NUMBER"
    NTILE = "NTILE"
    FIRST_VALUE = "FIRST_VALUE"
    LAST_VALUE = "LAST_VALUE"
    AVG = "AVG"
    COUNT = "COUNT"
    SUM = "SUM"
    MAX = "MAX"
    MIN = "MIN"
    LAG = "LAG"
    LEAD = "LEAD"
    CUME_DIST = "CUME_DIST"
    PERCENT_RANK = "PERCENT_RANK"
    NTH_VALUE = "NTH_VALUE"
