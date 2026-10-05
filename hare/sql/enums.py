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


class Order(StrEnum):
    """Sort direction, optionally with an explicit NULL placement - the plain members leave the
    NULL placement to the dialect's own default."""

    ASC = "ASC"
    DESC = "DESC"
    ASC_NULLS_FIRST = "ASC NULLS FIRST"
    ASC_NULLS_LAST = "ASC NULLS LAST"
    DESC_NULLS_FIRST = "DESC NULLS FIRST"
    DESC_NULLS_LAST = "DESC NULLS LAST"

    @property
    def is_ascending(self) -> bool:
        return self.value.startswith("ASC")

    @property
    def nulls_first(self) -> bool | None:
        """True/False for an explicit NULLS FIRST/NULLS LAST, None when left to the dialect."""
        if self.value.endswith("NULLS FIRST"):
            return True
        if self.value.endswith("NULLS LAST"):
            return False
        return None

    @classmethod
    def build(cls, is_ascending: bool, nulls_first: bool | None = None) -> Order:
        """Builds the member for a direction and an optional explicit NULL placement.

        Args:
            is_ascending: True for ASC, False for DESC.
            nulls_first: True for NULLS FIRST, False for NULLS LAST, None to leave it to the dialect.
        """
        value = "ASC" if is_ascending else "DESC"
        if nulls_first is not None:
            value += " NULLS FIRST" if nulls_first else " NULLS LAST"
        return cls(value)

    def get_reversed(self) -> Order:
        """The exact opposite ordering: the direction is inverted and an explicit NULL placement
        is inverted with it, so the reversed sequence is precisely the original read backwards."""
        nulls_first = self.nulls_first
        return Order.build(not self.is_ascending, None if nulls_first is None else not nulls_first)


class JoinType(StrEnum):
    INNER = ""
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    OUTER = "FULL OUTER"
    LEFT_OUTER = "LEFT OUTER"
    CROSS = "CROSS"
    #: Each row joined with the one row of the other table closest to it by an inequality, after
    #: the equalities - or with NULLs when none is.
    ASOF_LEFT = "ASOF LEFT"
    #: Each row repeated with each element of an array of it - no row for an empty array, or (LEFT) a
    #: row with the element type's default.
    ARRAY = "ARRAY"
    LEFT_ARRAY = "LEFT ARRAY"


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
    """How a value is written into a JSON object built on SQLite - as Postgres's jsonb holds it."""

    PLAIN = "plain"
    BOOLEAN = "boolean"
    DECIMAL = "decimal"
    FLOAT = "float"
    DATETIME = "datetime"
    TIME = "time"
    BINARY = "binary"
    JSON = "json"


class TruncType(StrEnum):
    """What ``Trunc()`` keeps of a date/time - a ``DATE_TRUNC`` unit on Postgres, the SQLite UDF's
    type; ``date``/``time`` take a datetime's date or time of day."""

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
    """The type of value ``Trunc()`` truncates."""

    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"


class DatetimeCastTarget(StrEnum):
    """What a datetime is cast to - the SQL type name on Postgres, the SQLite UDF's part name."""

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
    STDDEV_POP = "STDDEV_POP"
    STDDEV_SAMP = "STDDEV_SAMP"
    VAR_POP = "VAR_POP"
    VAR_SAMP = "VAR_SAMP"
    COUNT = "COUNT"
    SUM = "SUM"
    MAX = "MAX"
    MIN = "MIN"
    LAG = "LAG"
    LEAD = "LEAD"
    CUME_DIST = "CUME_DIST"
    PERCENT_RANK = "PERCENT_RANK"
    NTH_VALUE = "NTH_VALUE"


class JSONOperators(StrEnum):
    HAS_KEY = "?"
    CONTAINS = "@>"
    CONTAINED_BY = "<@"
    HAS_KEYS = "?&"
    HAS_ANY_KEYS = "?|"
    GET_JSON_VALUE = "->"
    GET_TEXT_VALUE = "->>"
