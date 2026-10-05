from __future__ import annotations

import datetime
import re

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DATETIME_PRECISION,
    CLICKHOUSE_FIRST_YEAR,
    CLICKHOUSE_LAST_YEAR,
)
from hare.sql.enums import AnalyticFunctionName, Order

#: A ClickHouse integer type, nullable or not - ``UInt64``, ``Nullable(Int8)``.
CLICKHOUSE_INTEGER_TYPE_PATTERN = re.compile(r"(?:Nullable\()?(?P<unsigned>U?)Int(?P<bits>\d+)\)?")
#: The first and the last microsecond since 1970 a ``DateTime64`` column holds.
CLICKHOUSE_FIRST_MICROSECOND = (
    datetime.datetime(CLICKHOUSE_FIRST_YEAR, 1, 1) - datetime.datetime(1970, 1, 1)
) // datetime.timedelta(microseconds=1)
CLICKHOUSE_LAST_MICROSECOND = (
    datetime.datetime(CLICKHOUSE_LAST_YEAR + 1, 1, 1) - datetime.datetime(1970, 1, 1)
) // datetime.timedelta(microseconds=1) - 1

#: A moment shifted by whole microseconds, refused beyond the moments ClickHouse holds - its own
#: functions stop at the last one. The column holds its moment as microseconds, compared with the
#: bounds less the shift, which the server computes once. {0}: the moment, {1}: the shift as an
#: ``Int64``, {2}: the error.
CLICKHOUSE_SHIFTED_MOMENT_TEMPLATE = (
    "addMicroseconds({0}, {1} + throwIf(ifNull(abs({1}) > "
    + str(CLICKHOUSE_LAST_MICROSECOND - CLICKHOUSE_FIRST_MICROSECOND)
    + " OR toUnixTimestamp64Micro({0}) > "
    + str(CLICKHOUSE_LAST_MICROSECOND)
    + " - {1} OR toUnixTimestamp64Micro({0}) < "
    + str(CLICKHOUSE_FIRST_MICROSECOND)
    + " - {1}, 0), '{2}'))"
)

#: The error of a moment shifted beyond the moments ClickHouse holds - it would roll over.
CLICKHOUSE_TIMESTAMP_OUT_OF_RANGE_MESSAGE = "timestamp out of range"

#: The error of a date shifted beyond the dates ClickHouse holds - it would roll over.
CLICKHOUSE_DATE_OUT_OF_RANGE_MESSAGE = "date out of range"

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
    "LPAD": "leftPadUTF8",
    "RPAD": "rightPadUTF8",
    "REPLACE": "replaceAll",
}

#: hare's digest text functions - ClickHouse returns the digest's bytes, written as lowercase hex.
CLICKHOUSE_DIGEST_FUNCTIONS = frozenset({"MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512"})

#: hare's math functions by the name hare writes them under -> ClickHouse's.
CLICKHOUSE_MATH_FUNCTION_NAMES = {
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

#: An integer result refused out of its type's range, the result written once: {0} the result,
#: {1} and {2} the type's least and greatest values, {3} the type as the error names it.
CLICKHOUSE_CHECKED_INTEGER_TEMPLATE = (
    "arrayMap(x -> x + throwIf(ifNull(x < {1} OR x > {2}, 0), '{3} out of range'), [{0}])[1]"
)

#: A number written into an integer enum column, refused when no member has it - a mutation writes it
#: even through a CAST to the column's type. {0}: the number, {1}: the members' numbers.
CLICKHOUSE_ASSIGNED_ENUM_TEMPLATE = (
    "arrayMap(x -> if(throwIf(ifNull(x NOT IN ({1}), 0), 'the value is the number of no member of the enum') = 0, "
    "x, x), [{0}])[1]"
)

#: hare's math functions ClickHouse has none of, or writes otherwise -> its SQL of them. ``{0}`` and ``{1}``
#: are the arguments. ``abs()`` of a signed integer is unsigned there - no common type with a signed one in
#: Coalesce()/Case() - so it is cast back to its argument's type, refused for the type's minimum, whose
#: absolute value the type doesn't hold (a plain CAST would wrap it round to the minimum again).
CLICKHOUSE_MATH_FUNCTION_TEMPLATES = {
    "LOG": "(log({1})/log({0}))",
    "COT": "(1/tan({0}))",
    "ABS": "accurateCast(abs({0}), toTypeName({0}))",
}

#: A character of a code point, written as its UTF-8 bytes - ``char()`` takes bytes; a code point no
#: character has is refused, its bytes would be no UTF-8. The code point is bound once (``n``) - a NULL's
#: as 1, its result left out.
CLICKHOUSE_CHARACTER_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(n -> if("
    "throwIf(n < 0 OR n > 1114111 OR (n >= 55296 AND n <= 57343), 'no character has the code point') = 0, "
    "multiIf(n < 128, char(n), n < 2048, char(192 + intDiv(n, 64), 128 + n % 64), "
    "n < 65536, char(224 + intDiv(n, 4096), 128 + intDiv(n, 64) % 64, 128 + n % 64), "
    "char(240 + intDiv(n, 262144), 128 + intDiv(n, 4096) % 64, 128 + intDiv(n, 64) % 64, 128 + n % 64)), ''), "
    "[toInt64(ifNull({0}, 1))])[1])"
)

#: The code point of a text's first character, decoded from its UTF-8 bytes - ``ascii()`` gives the
#: first byte. The character is bound once (``c``); a NULL text gives NULL, an empty one 0.
CLICKHOUSE_CODE_POINT_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(c -> multiIf(length(c) = 0, 0, length(c) = 1, ascii(c), "
    "length(c) = 2, bitAnd(ascii(c), 31) * 64 + bitAnd(ascii(substring(c, 2, 1)), 63), "
    "length(c) = 3, bitAnd(ascii(c), 15) * 4096 + bitAnd(ascii(substring(c, 2, 1)), 63) * 64 "
    "+ bitAnd(ascii(substring(c, 3, 1)), 63), "
    "bitAnd(ascii(c), 7) * 262144 + bitAnd(ascii(substring(c, 2, 1)), 63) * 4096 "
    "+ bitAnd(ascii(substring(c, 3, 1)), 63) * 64 + bitAnd(ascii(substring(c, 4, 1)), 63)), "
    "[substringUTF8(toString(ifNull({0}, '')), 1, 1)])[1])"
)

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

#: The type the operands of an integer sum, difference or product a query reads are cast to - wide
#: enough for the exact result of 64-bit operands; a written one is the server's.
CLICKHOUSE_EXACT_INTEGER_TYPE = "'Int256'"
#: The same for an operand that can be NULL.
CLICKHOUSE_NULLABLE_EXACT_INTEGER_TYPE = "'Nullable(Int256)'"

#: The scale of a decimal of any scale - the widest decimal's, half its digits after the point.
CLICKHOUSE_NUMERIC_SCALE = 38

#: A value cast to a decimal of any scale - the widest decimal of ``CLICKHOUSE_NUMERIC_SCALE``.
CLICKHOUSE_NUMERIC_CAST_TYPE = f"toDecimal256({{}}, {CLICKHOUSE_NUMERIC_SCALE})"

#: The functions whose ClickHouse result is a UInt64 - an integer of hare's, signed: ClickHouse finds
#: no common type of a UInt64 and a signed integer in COALESCE, CASE or greatest().
CLICKHOUSE_UNSIGNED_RESULT_FUNCTION_NAMES = frozenset({"LENGTH", "CHAR_LENGTH", "STRPOS", "COUNT"})
#: The signed integer of such a function's result.
CLICKHOUSE_SIGNED_RESULT_TEMPLATE = "toInt64({0})"
#: The window functions whose ClickHouse result is a UInt64 - written signed, as the functions above.
CLICKHOUSE_UNSIGNED_RESULT_WINDOW_FUNCTION_NAMES = (
    AnalyticFunctionName.ROW_NUMBER,
    AnalyticFunctionName.RANK,
    AnalyticFunctionName.DENSE_RANK,
    AnalyticFunctionName.NTILE,
)

#: The functions of plain SQL's names hare writes as they are -> ClickHouse's own, counting
#: characters.
CLICKHOUSE_GENERIC_FUNCTION_NAMES = {
    "LENGTH": "lengthUTF8",
    "CHAR_LENGTH": "lengthUTF8",
    "UPPER": "upperUTF8",
    "LOWER": "lowerUTF8",
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

#: The ``formatDateTime`` pattern of a time of day written as text, with its microseconds.
CLICKHOUSE_TIME_TEXT_FORMAT = "%H:%i:%S.%f"
#: The error of a text naming a clock no day has - ClickHouse would roll ``25:00`` over into the next
#: day, ``12:60`` into the next hour.
CLICKHOUSE_IMPOSSIBLE_CLOCK_MESSAGE = "the text names an impossible time of day"
#: Whether the text ``t`` names an impossible clock: an hour past 23 (the first number of a clock, not
#: its minutes or seconds) or a minute or a second past 59.
CLICKHOUSE_IMPOSSIBLE_CLOCK_CONDITION = (
    "(match(t, '(^|[^0-9:.])(2[4-9]|[3-9][0-9]):[0-9]{2}') OR match(t, ':[6-9][0-9]'))"
)
#: A text read as a time of day - the moment of it on the first day of 1970 in UTC, an offset it
#: names taken. A text not starting with its hours and minutes is refused, as is an impossible clock -
#: neither is read as some time of day. A NULL's text is read as midnight, its result left out.
CLICKHOUSE_TIME_OF_TEXT_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(t -> if(throwIf("
    + CLICKHOUSE_IMPOSSIBLE_CLOCK_CONDITION.replace("{", "{{").replace("}", "}}")
    + f", '{CLICKHOUSE_IMPOSSIBLE_CLOCK_MESSAGE}') = 0, parseDateTime64BestEffort(if("
    "match(t, '^[0-9]{{1,2}}:[0-9]{{2}}'), concat('1970-01-01 ', t), ''), 6, 'UTC'), NULL), "
    "[trimBoth(ifNull({0}, '00:00'))])[1])"
)

#: The fraction of a whole second, cut off a time of day's text - the text then is the one
#: ``datetime.time.isoformat()`` writes, which a ``TimeField`` stores and a filter compares with.
CLICKHOUSE_WHOLE_SECOND_FRACTION_PATTERN = "\\\\.000000$"

#: The UTC offset an aware time of day's text ends with - ``+03:00``, ``-05:30:15``.
CLICKHOUSE_TIME_OFFSET_PATTERN = "[+-][0-9]{2}:[0-9]{2}(:[0-9]{2})?$"

#: The sample deviations - NULL in SQL over fewer than two values, NaN in ClickHouse over one.
CLICKHOUSE_SAMPLE_AGGREGATE_NAMES = frozenset({"STDDEV_SAMP", "VAR_SAMP"})
#: The function counting the values a sample deviation reads.
CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME = "count"

#: The aggregate collecting a group's values into an array, and the functions ClickHouse collects them
#: with - every value, and the distinct ones.
CLICKHOUSE_ARRAY_AGGREGATE_NAME = "ARRAY_AGG"
CLICKHOUSE_GROUP_ARRAY_FUNCTION_NAME = "groupArray"
CLICKHOUSE_GROUP_DISTINCT_ARRAY_FUNCTION_NAME = "groupUniqArray"

#: The aggregates whose result over no rows would be a value no row holds -> ClickHouse's ``-OrNull``
#: form of each, which returns NULL there instead of the column type's default (0, 1970-01-01) or NaN.
#: A sum over no rows is ClickHouse's own 0.
CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES = {
    "MAX": "maxOrNull",
    "MIN": "minOrNull",
    "AVG": "avgOrNull",
    "STDDEV_POP": "stddevPopOrNull",
    "STDDEV_SAMP": "stddevSampOrNull",
    "VAR_POP": "varPopOrNull",
    "VAR_SAMP": "varSampOrNull",
    "ARG_MAX": "argMaxOrNull",
    "ARG_MIN": "argMinOrNull",
    # ClickHouse's own aggregates of hare.dialects.clickhouse.functions, by their names.
    "argMax": "argMaxOrNull",
    "argMin": "argMinOrNull",
    "any": "anyOrNull",
    "anyLast": "anyLastOrNull",
    "quantile": "quantileOrNull",
    "quantileExact": "quantileExactOrNull",
}
#: The aggregates ClickHouse names otherwise in a grouped statement, where a group has a row.
CLICKHOUSE_GROUPED_AGGREGATE_NAMES = {"ARG_MAX": "argMax", "ARG_MIN": "argMin"}

#: The digits a Decimal256 holds - a sum or product past them wraps around without an error.
CLICKHOUSE_WIDE_DECIMAL_DIGITS = 76
#: The error of a Decimal past the digits its type holds - it would wrap around.
CLICKHOUSE_NUMERIC_OVERFLOW_MESSAGE = "the value has more digits than the decimal holds"

#: The name an average of Decimals is rendered under - ClickHouse's avg() of a Decimal is a Float64.
CLICKHOUSE_DECIMAL_AVERAGE_NAME = "HARE_DECIMAL_AVG"
#: The aggregates of Decimals taking the place of SQL's, by its name.
CLICKHOUSE_DECIMAL_AGGREGATE_NAMES = {"AVG": CLICKHOUSE_DECIMAL_AVERAGE_NAME}

#: A Decimal rounded to its target's places (``{1}``) half away from zero, refused when its whole part
#: has more digits than the target holds (``{2}`` is ten to their number), converted to the target type
#: (``{3}``).
CLICKHOUSE_DECIMAL_CAST_TEMPLATE = (
    "arrayMap(v -> if(throwIf(ifNull(abs(v) >= toDecimal256('{2}', 0), 0), "
    "'the value has more digits than the decimal holds') = 0, CAST(v AS {3}), NULL), [round({0}, {1})])[1]"
)

#: The error of a text naming a date outside the ones ClickHouse stores - it would read the nearest of them.
CLICKHOUSE_TEXT_DATE_OUT_OF_RANGE_MESSAGE = (
    f"the text names a date outside {CLICKHOUSE_FIRST_YEAR}-01-01 to {CLICKHOUSE_LAST_YEAR}-12-31, which "
    "ClickHouse would turn into the nearest of them"
)
#: A text read as a moment in UTC - an impossible date or clock is refused, not rolled over, and so is a
#: text naming no whole date (a year of four digits with a month and a day: three groups of digits, eight
#: digits in a row, or a month's name and two groups), which ClickHouse would complete with the current
#: year, month or day ("12" the 12th of January), and a date outside the years hare stores there - a
#: server before 26 reads it as the first or the last day it holds, a moment read as that day's midnight
#: from a text not naming its year.
#: A NULL's text is read as the epoch, its result left out.
CLICKHOUSE_TEXT_MOMENT_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(t -> arrayMap(m -> if(throwIf("
    + CLICKHOUSE_IMPOSSIBLE_CLOCK_CONDITION.replace("{", "{{").replace("}", "}}")
    + f", '{CLICKHOUSE_IMPOSSIBLE_CLOCK_MESSAGE}') + throwIf("
    f"toYear(m) < {CLICKHOUSE_FIRST_YEAR} OR toYear(m) > {CLICKHOUSE_LAST_YEAR} OR (m IN ("
    f"toDateTime64('{CLICKHOUSE_FIRST_YEAR}-01-01', 6, {{1}}), "
    f"toDateTime64('{CLICKHOUSE_LAST_YEAR}-12-31', 6, {{1}})) "
    f"AND position(t, toString(toYear(m))) = 0), '{CLICKHOUSE_TEXT_DATE_OUT_OF_RANGE_MESSAGE}') = 0, m, NULL), "
    "[parseDateTime64BestEffort(if(match(t, '[0-9]{{4}}') AND (length(extractAll(t, '[0-9]+')) >= 3 "
    "OR match(t, '[0-9]{{8}}') OR (match(t, '[A-Za-z]{{3}}') AND length(extractAll(t, '[0-9]+')) >= 2)), "
    "t, ''), 6, {1})])[1], [ifNull({0}, '1970-01-01')])[1])"
)

#: A text moment's clock without the UTC offset after it - a naive timestamp reads its text's own clock.
CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE = (
    "replaceRegexpOne({0}, '(\\\\d{{2}}:\\\\d{{2}}(:\\\\d{{2}}(\\\\.\\\\d+)?)?)"
    "\\\\s*(Z|[+-]\\\\d{{1,2}}(:?\\\\d{{2}})?)$', '\\\\1')"
)

#: The window functions reading a neighboring row -> ClickHouse's, which read the window's frame.
CLICKHOUSE_OFFSET_FUNCTION_NAMES = {"LAG": "lagInFrame", "LEAD": "leadInFrame"}

#: A window frame of the whole partition.
CLICKHOUSE_WHOLE_PARTITION_FRAME = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"

#: The SQL of the sort directions ClickHouse writes otherwise - a descending one puts NULLs first, so
#: it is the ascending one reversed: ClickHouse puts them last in both directions, and reverse(),
#: last() and the pages after a cursor would lose or repeat the rows holding them.
CLICKHOUSE_ORDER_SQLS = {Order.DESC: "DESC NULLS FIRST"}

#: The key of a window ordering whose frame is a range of values - the ordered value as a float, a
#: NULL as an infinity at the end NULLs sort to (``{1}``: ``inf``/``-inf``).
CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE = "ifNull(toFloat64({0}), {1})"

#: A window frame of the rows up to the current one and its peers.
CLICKHOUSE_UP_TO_PEERS_FRAME = "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"

#: The JSON text of a value - ``null`` for NULL.
CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE = "ifNull(toJSONString({0}), 'null')"

#: The JSON text of a JSON value nested - its own text.
CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE = "ifNull(toString({0}), 'null')"

#: Bytes as a JSON string of their hex.
CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE = "if(isNull({0}), 'null', concat('\"', hex({0}), '\"'))"

#: The current moment - a DateTime64 of microseconds in UTC, as a moment column holds it.
CLICKHOUSE_NOW_SQL = f"now64({CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"

#: The zone an aware moment is written and read in.
CLICKHOUSE_UTC_ZONE_SQL = "'UTC'"
