from __future__ import annotations

import datetime
import re

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DATETIME_PRECISION,
    CLICKHOUSE_FIRST_YEAR,
    CLICKHOUSE_LAST_YEAR,
)
from hare.sql.enums import CastType, Order

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

#: The error of a moment shifted beyond the moments ClickHouse holds - PostgreSQL's.
CLICKHOUSE_TIMESTAMP_OUT_OF_RANGE_MESSAGE = "timestamp out of range"

#: The error of a date shifted beyond the dates ClickHouse holds - PostgreSQL's.
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

#: e to the power of a float, within a unit in the last place: e^x = 2^k * e^r with k the nearest integer to
#: x / ln 2 and r = x - k ln 2 computed with ln 2 split in two (Cody and Waite), so that pow() of e takes
#: an argument below 0.35; 2^k is applied in two halves, not to overflow before the product does.
CLICKHOUSE_EXPONENTIAL_TEMPLATE = (
    "if(isFinite({0}), arrayMap(k -> exp2(floor(k / 2)) * (exp2(k - floor(k / 2)) "
    "* pow(e(), ({0} - k * 0.6931471803691238) - k * 1.9082149292705877e-10)), "
    "[round({0} / 0.6931471805599453)])[1], exp({0}))"
)

#: hare's math functions defined for part of their arguments only -> ClickHouse's SQL of them, which
#: refuses the others with PostgreSQL's error where ClickHouse gives an infinity or NaN. ``{0}`` and
#: ``{1}`` are the arguments, a float's taken as Float64 - ClickHouse has no arithmetic of a Decimal
#: and a float. A condition is checked through ``ifNull()``: ``throwIf()`` reads the value
#: a NULL row holds underneath. ClickHouse's own ``log()`` and ``exp()`` are off by up to millions of
#: units in the last place: a logarithm is taken through ``log2()``, ``exp()`` is
#: ``CLICKHOUSE_EXPONENTIAL_TEMPLATE`` - both within one unit, as the C library's.
CLICKHOUSE_MATH_DOMAIN_TEMPLATES = {
    "LN": (
        "(log2({0})/log2(e()) + throwIf(ifNull({0} = 0, 0), 'cannot take logarithm of zero') "
        "+ throwIf(ifNull({0} < 0, 0), 'cannot take logarithm of a negative number'))"
    ),
    "LOG": (
        "(log2({1})/log2({0}) + throwIf(ifNull({0} = 0 OR {1} = 0, 0), 'cannot take logarithm of zero') "
        "+ throwIf(ifNull({0} < 0 OR {1} < 0, 0), 'cannot take logarithm of a negative number') "
        "+ throwIf(ifNull({0} = 1, 0), 'division by zero'))"
    ),
    "DIVISOR": "({1} + throwIf(ifNull({1} = 0 AND isNotNull({0}), 0), 'division by zero'))",
    "SQRT": "(sqrt({0}) + throwIf(ifNull({0} < 0, 0), 'cannot take square root of a negative number'))",
    "ASIN": "(asin({0}) + throwIf(ifNull({0} < -1 OR {0} > 1, 0), 'input is out of range'))",
    "ACOS": "(acos({0}) + throwIf(ifNull({0} < -1 OR {0} > 1, 0), 'input is out of range'))",
    "EXP": (
        "arrayMap(x -> arrayMap(v -> v "
        "+ throwIf(ifNull(isInfinite(v) AND isFinite(x), 0), 'value out of range: overflow') "
        "+ throwIf(ifNull(v = 0 AND isFinite(x), 0), 'value out of range: underflow'), "
        "[" + CLICKHOUSE_EXPONENTIAL_TEMPLATE.format("x") + "])[1], [toFloat64({0})])[1]"
    ),
    "POWER": (
        "arrayMap((b, x) -> arrayMap(p -> p "
        "+ throwIf(ifNull(b = 0 AND x < 0, 0), 'zero raised to a negative power is undefined') "
        "+ throwIf(ifNull(b < 0 AND x != floor(x), 0), "
        "'a negative number raised to a non-integer power yields a complex result') "
        "+ throwIf(ifNull(isInfinite(p) AND isFinite(b) AND isFinite(x), 0), 'value out of range: overflow') "
        "+ throwIf(ifNull(p = 0 AND b != 0 AND isFinite(b) AND isFinite(x), 0), 'value out of range: underflow'), "
        "[pow(b, x)])[1], [toFloat64({0})], [toFloat64({1})])[1]"
    ),
}

#: A float rounded half away from zero, as SQLite rounds its double - ``round()`` rounds a float's half
#: to even. ``{1}`` is the places, ``{2}`` ten to their power.
CLICKHOUSE_FLOAT_ROUND_TEMPLATE = (
    "if(abs({0} * {2} - trunc({0} * {2})) = 0.5, (trunc({0} * {2}) + sign({0})) / {2}, round({0},{1}))"
)

#: A character of a code point, written as its UTF-8 bytes - ``char()`` takes bytes; a code point no
#: character has is refused with PostgreSQL's error. The code point is bound once (``n``) - a NULL's as
#: 1, its result left out.
CLICKHOUSE_CHARACTER_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(n -> if("
    "throwIf(n = 0, 'null character not permitted') + throwIf(n < 0, 'character number must be positive') "
    "+ throwIf(n > 1114111, 'requested character too large for encoding') "
    "+ throwIf(n >= 55296 AND n <= 57343, 'requested character not valid for encoding') = 0, "
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

#: A substring by a 1-based start and a length, as PostgreSQL's - a start before 1 counts the positions
#: before the text; a negative length is refused.
CLICKHOUSE_SUBSTRING_TEMPLATE = (
    "substringUTF8(toString({0}), greatest({1}, 1), "
    "greatest({1} + {2}, 1) - greatest({1}, 1) "
    "+ throwIf(ifNull({2} < 0, 0), 'negative substring length not allowed'))"
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

#: The type the operands of an integer sum, difference or product written into a column are cast
#: to - wide enough for the exact result of 64-bit operands.
CLICKHOUSE_EXACT_INTEGER_TYPE = "'Int256'"

#: The scale of a decimal of any scale - the widest decimal's, half its digits after the point.
CLICKHOUSE_NUMERIC_SCALE = 38

#: A value cast to a decimal of any scale - the widest decimal of ``CLICKHOUSE_NUMERIC_SCALE``.
CLICKHOUSE_NUMERIC_CAST_TYPE = f"toDecimal256({{}}, {CLICKHOUSE_NUMERIC_SCALE})"

#: The functions of plain SQL's names hare writes as they are -> ClickHouse's own, counting
#: characters.
CLICKHOUSE_GENERIC_FUNCTION_NAMES = {
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

#: The UTC offset an aware time of day's text ends with - ``+03:00``, ``-05:30:15``.
CLICKHOUSE_TIME_OFFSET_PATTERN = "[+-][0-9]{2}:[0-9]{2}(:[0-9]{2})?$"

#: The zeros a moment's fraction of a second ends with - cut off its text, as PostgreSQL writes it:
#: ``.5``, no fraction for a whole second.
CLICKHOUSE_MOMENT_FRACTION_ZEROS_PATTERN = "\\\\.?0+$"
#: The offset an aware moment's text ends with - PostgreSQL writes UTC's in hours alone.
CLICKHOUSE_UTC_OFFSET_TEXT = "+00"
#: A time of day's text as PostgreSQL writes it: no trailing zeros in its fraction, an offset of whole
#: hours without its minutes.
CLICKHOUSE_TIME_TEXT_TEMPLATE = (
    "replaceRegexpOne(replaceRegexpOne(replaceRegexpOne(toString({0}), '(\\\\.\\\\d*[1-9])0+([+-]|$)', "
    "'\\\\1\\\\2'), '\\\\.0+([+-]|$)', '\\\\1'), '([+-]\\\\d\\\\d):00$', '\\\\1')"
)
#: A decimal's text with every place of its type - ``toString()`` drops the trailing zeros, and they
#: are put back up to the type's scale (``toDecimalString()`` takes the scale only as a constant, which
#: the type of an aggregate isn't); a value of another type (a float an expression gives) as
#: ``toString()`` writes it - its type names no scale.
CLICKHOUSE_DECIMAL_TEXT_TEMPLATE = (
    "concat(toString({0}), if(position(toString({0}), '.') = 0 AND {1} > 0, '.', ''), "
    "repeat('0', toUInt64(greatest(0, {1} - if(position(toString({0}), '.') = 0, 0, "
    "length(toString({0})) - position(toString({0}), '.'))))))"
)
#: The scale a value's type names - 0 for a type naming none.
CLICKHOUSE_TYPE_SCALE_TEMPLATE = "toInt64(toUInt8OrZero(extract(toTypeName({0}), ',\\\\s*(\\\\d+)\\\\)')))"
#: A float's text as PostgreSQL writes it: ``NaN``, ``Infinity``; the shortest digits that read back
#: as the float - ``toString()``'s - positional from 1e-4 to 1e15, else scientific with a signed exponent
#: of two digits at least (``1.5e-07``, ``1e+308``). The value is bound once (``v``), the digits and the exponent
#: of a scientific one read off ``toString()`` (``p``) - a NULL is no value to bind (a Nullable value
#: holds no array).
CLICKHOUSE_FLOAT_TEXT_TEMPLATE = (
    "if(isNull({0}), NULL, arrayMap(v -> multiIf(isNaN(v), 'NaN', isInfinite(v), if(v > 0, 'Infinity', '-Infinity'), "
    "v = 0 OR (abs(v) >= 0.0001 AND abs(v) < 1e15), toString(v), "
    "arrayMap(p -> concat(if(v < 0, '-', ''), substring(p.1, 1, 1), "
    "if(length(p.1) > 1, concat('.', substring(p.1, 2)), ''), 'e', if(p.2 < 0, '-', '+'), "
    "if(abs(p.2) < 10, '0', ''), toString(abs(p.2))), "
    "[arrayMap(t -> if(position(t, 'e') > 0, "
    "(replaceAll(substring(t, 1, position(t, 'e') - 1), '.', ''), toInt32(substring(t, position(t, 'e') + 1))), "
    "if(startsWith(t, '0.'), "
    "(replaceRegexpOne(substring(t, 3), '^0+', ''), "
    "toInt32(-1 - length(replaceRegexpOne(substring(t, 3), '[1-9].*$', '')))), "
    "(replaceRegexpOne(replaceAll(t, '.', ''), '0+$', ''), toInt32(length(splitByChar('.', t)[1]) - 1)))), "
    "[toString(abs(v))])[1]])[1]), [assumeNotNull({0})])[1])"
)

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
    "ARG_MAX": "argMaxOrNull",
    "ARG_MIN": "argMinOrNull",
}
#: The aggregates ClickHouse names otherwise in a grouped statement, where a group has a row.
CLICKHOUSE_GROUPED_AGGREGATE_NAMES = {"ARG_MAX": "argMax", "ARG_MIN": "argMin"}

#: The scale a Decimal dividend is widened to - ClickHouse's Decimal quotient has the dividend's scale.
CLICKHOUSE_QUOTIENT_SCALE = 20

#: The types a value converts from to each type, as PostgreSQL casts it - a cast from any other type is
#: refused (a boolean converts to a 32-bit integer only). A text target takes any value.
CLICKHOUSE_CAST_SOURCES = {
    CastType.INTEGER: frozenset(
        {CastType.INTEGER, CastType.FLOAT, CastType.DECIMAL, CastType.TEXT, CastType.BOOLEAN, CastType.UNKNOWN}
    ),
    CastType.FLOAT: frozenset({CastType.INTEGER, CastType.FLOAT, CastType.DECIMAL, CastType.TEXT, CastType.UNKNOWN}),
    CastType.DECIMAL: frozenset({CastType.INTEGER, CastType.FLOAT, CastType.DECIMAL, CastType.TEXT, CastType.UNKNOWN}),
    CastType.BOOLEAN: frozenset({CastType.INTEGER, CastType.BOOLEAN, CastType.TEXT, CastType.UNKNOWN}),
    CastType.DATE: frozenset({CastType.DATE, CastType.DATETIME, CastType.TEXT, CastType.UNKNOWN}),
    CastType.DATETIME: frozenset({CastType.DATE, CastType.DATETIME, CastType.TEXT, CastType.UNKNOWN}),
    CastType.TIME: frozenset({CastType.DATETIME, CastType.TIME, CastType.TEXT, CastType.UNKNOWN}),
}

#: The bits of the only integer a boolean converts to.
CLICKHOUSE_BOOLEAN_CAST_INTEGER_BITS = 32

#: PostgreSQL's name of each type a value converts from or to - for the error of a refused cast.
CLICKHOUSE_CAST_TYPE_NAMES = {
    CastType.INTEGER: "integer",
    CastType.FLOAT: "double precision",
    CastType.DECIMAL: "numeric",
    CastType.TEXT: "text",
    CastType.BOOLEAN: "boolean",
    CastType.DATE: "date",
    CastType.DATETIME: "timestamp with time zone",
    CastType.TIME: "time without time zone",
    CastType.UNKNOWN: "unknown",
}

#: PostgreSQL's name of an integer of each size in bits.
CLICKHOUSE_INTEGER_TYPE_NAMES = {16: "smallint", 32: "integer", 64: "bigint"}

#: A text without its leading and trailing white space, as PostgreSQL reads a number or a boolean.
CLICKHOUSE_TRIMMED_TEXT_TEMPLATE = "replaceRegexpAll({0}, '^\\\\s+|\\\\s+$', '')"

#: A float as the Decimal PostgreSQL converts it to - its 15 significant digits: its mantissa scaled by
#: an exact power of ten (``e``), rounded to 14 places and written back with the exponent. A zero's or a
#: NULL's exponent is taken of 1.
CLICKHOUSE_FLOAT_DECIMAL_TEMPLATE = (
    "if({0} = 0, toDecimal256(0, 38), arrayMap(e -> toDecimal256(concat(toString(roundBankers(toDecimal256("
    "toString(if(e >= 0, {0} / exp10(e), {0} * exp10(-e))), 38), 14)), 'e', toString(e)), 38), "
    "[toInt64(floor(log10(abs(if(ifNull({0}, 0) = 0, 1, {0})))))])[1])"
)

#: A Decimal rounded to its target's places (``{1}``) half away from zero, refused with PostgreSQL's
#: error when its whole part has more digits than the target holds (``{2}`` is ten to their number),
#: converted to the target type (``{3}``).
CLICKHOUSE_DECIMAL_CAST_TEMPLATE = (
    "arrayMap(v -> if(throwIf(ifNull(abs(v) >= toDecimal256('{2}', 0), 0), 'numeric field overflow') = 0, "
    "CAST(v AS {3}), NULL), [round({0}, {1})])[1]"
)

#: PostgreSQL's boolean input - ``true``/``yes`` and their prefixes, ``on``, ``1``; their opposites and ``of`` -
#: of a trimmed lowercase text (``t``); any other text is refused.
CLICKHOUSE_TEXT_BOOLEAN_TEMPLATE = (
    "arrayMap(t -> if(isNull(t), NULL, toBool("
    "(t != '' AND (startsWith('true', t) OR startsWith('yes', t) OR t IN ('on', '1'))) "
    "+ throwIf(ifNull(NOT ((t != '' AND (startsWith('true', t) OR startsWith('yes', t) OR t IN ('on', '1'))) "
    "OR (t != '' AND (startsWith('false', t) OR startsWith('no', t) OR t IN ('of', 'off', '0')))), 0), "
    "'invalid input syntax for type boolean'))), [lowerUTF8({0})])[1]"
)

#: A text read as a moment in UTC - an impossible date is refused, not rolled over. A NULL's text is
#: read as the epoch, its result left out.
CLICKHOUSE_TEXT_MOMENT_TEMPLATE = (
    "if(isNull({0}), NULL, parseDateTime64BestEffort(ifNull({0}, '1970-01-01'), 6, 'UTC'))"
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

#: The SQL of the sort directions ClickHouse writes otherwise - a descending one puts NULLs first, as
#: PostgreSQL does, where ClickHouse puts them last in both directions.
CLICKHOUSE_ORDER_SQLS = {Order.DESC: "DESC NULLS FIRST"}

#: The key of a window ordering whose frame is a range of values - the ordered value as a float, a
#: NULL as an infinity at the end NULLs sort to (``{1}``: ``inf``/``-inf``).
CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE = "ifNull(toFloat64({0}), {1})"

#: A window frame of the rows up to the current one and its peers.
CLICKHOUSE_UP_TO_PEERS_FRAME = "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"

#: The JSON text of a value of no particular JSON type - an integer as its digits (``toJSONString()``
#: quotes a 64-bit one on older servers), any other value by ``toJSONString()``; NULL as ``null``.
CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE = (
    "multiIf(isNull({0}), 'null', match(toTypeName({0}), '^(Nullable\\()?U?Int'), toString({0}), toJSONString({0}))"
)

#: The JSON text of a boolean.
CLICKHOUSE_JSON_BOOLEAN_TEXT_TEMPLATE = "multiIf(isNull({0}), 'null', {0}, 'true', 'false')"

#: The JSON text of a value written as its own text - a Decimal, a JSON value nested.
CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE = "ifNull(toString({0}), 'null')"

#: A JSON value's text with a space after each ``,`` and ``:`` outside its strings, as PostgreSQL's
#: ``jsonb`` writes it - the text split into its strings, its separators and the runs between them.
CLICKHOUSE_JSON_AS_TEXT_TEMPLATE = (
    "if(isNull({0}), NULL, arrayStringConcat(arrayMap(part -> multiIf(part = ',', ', ', part = ':', ': ', part), "
    r"""extractAll(ifNull(toString({0}), ''), '"(?:[^"\\\\]|\\\\.)*"|[^",:]+|[,:]'))))"""
)

#: The JSON text of a float - in its shortest digits, as PostgreSQL's jsonb holds a double; NaN and the
#: infinities as strings.
CLICKHOUSE_JSON_FLOAT_TEXT_TEMPLATE = (
    "multiIf(isNull({0}), 'null', isNaN({0}), '\"NaN\"', isInfinite({0}), "
    "if({0} > 0, '\"Infinity\"', '\"-Infinity\"'), toString({0}))"
)

#: The JSON string of a value's text (``{1}``).
CLICKHOUSE_JSON_STRING_TEMPLATE = "if(isNull({0}), 'null', concat('\"', {1}, '\"'))"

#: Bytes as PostgreSQL's text of them in a JSON string - a backslash, escaped, ``x`` and their hex.
CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE = "concat('\\\\\\\\x', lower(hex({0})))"

#: The current moment - a DateTime64 of microseconds in UTC, as a moment column holds it.
CLICKHOUSE_NOW_SQL = f"now64({CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"

#: The characters whose full uppercase is several characters (``ß`` -> ``SS``), which ``upperUTF8()``
#: writes - PostgreSQL maps case character by character.
CLICKHOUSE_EXPANDING_UPPERCASE_CHARACTERS = (
    "\u00df\u0149\u01f0\u0390\u03b0\u0587\u1e96\u1e97\u1e98\u1e99\u1e9a\u1f50\u1f52\u1f54\u1f56\u1f80\u1f81\u1f82"
    "\u1f83\u1f84\u1f85\u1f86\u1f87\u1f88\u1f89\u1f8a\u1f8b\u1f8c\u1f8d\u1f8e\u1f8f\u1f90\u1f91\u1f92\u1f93\u1f94"
    "\u1f95\u1f96\u1f97\u1f98\u1f99\u1f9a\u1f9b\u1f9c\u1f9d\u1f9e\u1f9f\u1fa0\u1fa1\u1fa2\u1fa3\u1fa4\u1fa5\u1fa6"
    "\u1fa7\u1fa8\u1fa9\u1faa\u1fab\u1fac\u1fad\u1fae\u1faf\u1fb2\u1fb3\u1fb4\u1fb6\u1fb7\u1fbc\u1fc2\u1fc3\u1fc4"
    "\u1fc6\u1fc7\u1fcc\u1fd2\u1fd3\u1fd6\u1fd7\u1fe2\u1fe3\u1fe4\u1fe6\u1fe7\u1ff2\u1ff3\u1ff4\u1ff6\u1ff7\u1ffc"
    "\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06\ufb13\ufb14\ufb15\ufb16\ufb17"
)

#: The one-character uppercase of each of ``CLICKHOUSE_EXPANDING_UPPERCASE_CHARACTERS``, at the same
#: position - the character itself when it has none.
CLICKHOUSE_SIMPLE_UPPERCASE_CHARACTERS = (
    "\u00df\u0149\u01f0\u0390\u03b0\u0587\u1e96\u1e97\u1e98\u1e99\u1e9a\u1f50\u1f52\u1f54\u1f56\u1f88\u1f89\u1f8a"
    "\u1f8b\u1f8c\u1f8d\u1f8e\u1f8f\u1f88\u1f89\u1f8a\u1f8b\u1f8c\u1f8d\u1f8e\u1f8f\u1f98\u1f99\u1f9a\u1f9b\u1f9c"
    "\u1f9d\u1f9e\u1f9f\u1f98\u1f99\u1f9a\u1f9b\u1f9c\u1f9d\u1f9e\u1f9f\u1fa8\u1fa9\u1faa\u1fab\u1fac\u1fad\u1fae"
    "\u1faf\u1fa8\u1fa9\u1faa\u1fab\u1fac\u1fad\u1fae\u1faf\u1fb2\u1fbc\u1fb4\u1fb6\u1fb7\u1fbc\u1fc2\u1fcc\u1fc4"
    "\u1fc6\u1fc7\u1fcc\u1fd2\u1fd3\u1fd6\u1fd7\u1fe2\u1fe3\u1fe4\u1fe6\u1fe7\u1ff2\u1ffc\u1ff4\u1ff6\u1ff7\u1ffc"
    "\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06\ufb13\ufb14\ufb15\ufb16\ufb17"
)

#: A text in uppercase character by character, like PostgreSQL: a text holding a character whose full
#: uppercase expands is converted one character at a time, any other one by ``upperUTF8()`` as a whole.
CLICKHOUSE_UPPER_TEMPLATE = (
    "if(match(ifNull({0}, ''), '[" + "".join(CLICKHOUSE_EXPANDING_UPPERCASE_CHARACTERS) + "]'), "
    "arrayStringConcat(arrayMap(c -> transform(c, ["
    + ",".join(f"'{character}'" for character in CLICKHOUSE_EXPANDING_UPPERCASE_CHARACTERS)
    + "], ["
    + ",".join(f"'{character}'" for character in CLICKHOUSE_SIMPLE_UPPERCASE_CHARACTERS)
    + "], upperUTF8(c)), extractAll(ifNull({0}, ''), '(?s).'))), upperUTF8({0}))"
)

#: A text in lowercase character by character, like PostgreSQL: ``İ`` becomes a bare ``i`` (its full
#: lowercase adds a combining dot), and ``Σ`` is lowered alone - ``lowerUTF8()`` writes a word-final one
#: as ``ς``.
CLICKHOUSE_LOWER_TEMPLATE = (
    "if(match(ifNull({0}, ''), '[\u0130\u03a3]'), "
    "arrayStringConcat(arrayMap(c -> transform(c, ['\u0130'], ['i'], lowerUTF8(c)), "
    "extractAll(ifNull({0}, ''), '(?s).'))), lowerUTF8({0}))"
)

#: The function dividing integers to an integer, rounded towards zero.
CLICKHOUSE_INTEGER_DIVISION_FUNCTION_NAME = "intDiv"
