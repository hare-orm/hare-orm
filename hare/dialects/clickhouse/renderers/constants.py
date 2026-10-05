from __future__ import annotations

#: hare's text functions by the name hare writes them under -> ClickHouse's function counting
#: characters, not bytes, where it has both.
CLICKHOUSE_TEXT_FUNCTION_NAMES = {
    "TRIM": "trimBoth",
    "LTRIM": "trimLeft",
    "RTRIM": "trimRight",
    "LEFT": "leftUTF8",
    "RIGHT": "rightUTF8",
    "SUBSTR": "substringUTF8",
    "STRPOS": "positionUTF8",
    "REPEAT": "repeat",
    "REVERSE": "reverseUTF8",
    "CHR": "char",
    "ASCII": "ascii",
    "LPAD": "leftPadUTF8",
    "RPAD": "rightPadUTF8",
    "REPLACE": "replaceAll",
}

#: hare's digest text functions - ClickHouse returns the digest's bytes, written as lowercase hex.
CLICKHOUSE_DIGEST_FUNCTIONS = frozenset({"MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512"})

#: hare's math functions by the name hare writes them under -> ClickHouse's.
CLICKHOUSE_MATH_FUNCTION_NAMES = {
    "ABS": "abs",
    "CEIL": "ceil",
    "FLOOR": "floor",
    "SIGN": "sign",
    "MOD": "modulo",
    "POWER": "pow",
    "SQRT": "sqrt",
    "EXP": "exp",
    "LN": "log",
    "SIN": "sin",
    "COS": "cos",
    "TAN": "tan",
    "ASIN": "asin",
    "ACOS": "acos",
    "ATAN": "atan",
    "ATAN2": "atan2",
    "DEGREES": "degrees",
    "RADIANS": "radians",
}

#: A plain SQL type hare casts to -> ClickHouse's function converting to it, which keeps a NULL.
CLICKHOUSE_CAST_FUNCTIONS = {
    "VARCHAR": "toString",
    "CHAR": "toString",
    "FLOAT": "toFloat64",
    "REAL": "toFloat32",
    "BIGINT": "toInt64",
    "INTEGER": "toInt32",
    "BOOLEAN": "toBool",
}

#: The plain SQL type of a decimal of any scale.
CLICKHOUSE_NUMERIC_TYPE_NAME = "NUMERIC"

#: A value cast to a decimal of any scale - the widest decimal, half its digits after the point.
CLICKHOUSE_NUMERIC_CAST_TYPE = "toDecimal256({}, 38)"

#: The functions of plain SQL's names hare writes as they are -> ClickHouse's own, counting
#: characters and converting letters beyond ASCII.
CLICKHOUSE_GENERIC_FUNCTION_NAMES = {
    "LOWER": "lowerUTF8",
    "UPPER": "upperUTF8",
    "LENGTH": "lengthUTF8",
    "CHAR_LENGTH": "lengthUTF8",
    "NOW": "now64",
}

#: The function reading each part of a moment or a date, in Python's ``datetime`` meaning - the
#: week day counted from Sunday as 1 (mode 3), the ISO week day from Monday as 1.
CLICKHOUSE_DATE_PART_FUNCTIONS = {
    "YEAR": "toYear({})",
    "ISOYEAR": "toISOYear({})",
    "QUARTER": "toQuarter({})",
    "MONTH": "toMonth({})",
    "WEEK": "toISOWeek({})",
    "DOW": "toDayOfWeek({}, 3)",
    "ISODOW": "toDayOfWeek({})",
    "DAY": "toDayOfMonth({})",
    "HOUR": "toHour({})",
    "MINUTE": "toMinute({})",
    "SECOND": "toSecond({})",
    "MICROSECOND": "positiveModulo(toUnixTimestamp64Micro(toDateTime64({}, 6)), 1000000)",
}

#: The ``formatDateTime`` pattern of a moment written as text, with its microseconds.
CLICKHOUSE_DATETIME_TEXT_FORMAT = "%Y-%m-%d %H:%i:%S.%f"

#: The ``formatDateTime`` pattern of a time of day written as text, with its microseconds.
CLICKHOUSE_TIME_TEXT_FORMAT = "%H:%i:%S.%f"
#: The fraction of a whole second, cut off a time of day's text - the text then is the one
#: ``datetime.time.isoformat()`` writes, which a ``TimeField`` stores and a filter compares with.
CLICKHOUSE_WHOLE_SECOND_FRACTION_PATTERN = "\\\\.000000$"

#: The sample deviations - NULL in SQL over fewer than two values, NaN in ClickHouse over one.
CLICKHOUSE_SAMPLE_AGGREGATE_NAMES = frozenset({"STDDEV_SAMP", "VAR_SAMP"})
#: The function counting the values a sample deviation reads.
CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME = "count"

#: The aggregate collecting a group's values into an array, and the functions ClickHouse collects them
#: with - every value, and the distinct ones.
CLICKHOUSE_ARRAY_AGGREGATE_NAME = "ARRAY_AGG"
CLICKHOUSE_GROUP_ARRAY_FUNCTION_NAME = "groupArray"
CLICKHOUSE_GROUP_DISTINCT_ARRAY_FUNCTION_NAME = "groupUniqArray"

#: The aggregates of plain SQL's names whose result over no rows is NULL -> ClickHouse's ``-OrNull``
#: form of each, which returns NULL there too instead of the column type's default (0, 1970-01-01).
CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES = {
    "SUM": "sumOrNull",
    "MAX": "maxOrNull",
    "MIN": "minOrNull",
    "AVG": "avgOrNull",
    "STDDEV_POP": "stddevPopOrNull",
    "STDDEV_SAMP": "stddevSampOrNull",
    "VAR_POP": "varPopOrNull",
    "VAR_SAMP": "varSampOrNull",
}
