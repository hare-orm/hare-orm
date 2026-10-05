from __future__ import annotations

#: The name of the key existence test - no function of ClickHouse; the term writes its own SQL.
CLICKHOUSE_JSON_KEY_EXISTENCE_FUNCTION_NAME = "HARE_JSON_KEY_EXISTENCE"

#: Whether the JSON text ``{document}`` holds the key ``hare_json_key``, as PostgreSQL's jsonb ``?``
#: tests it. The lambda parameters are named as no column is, which they would hide.
CLICKHOUSE_JSON_HAS_KEY_SQL = (
    "multiIf(JSONType({document}) = 'Object', JSONHas({document}, hare_json_key), "
    "JSONType({document}) = 'Array', "
    "arrayExists(hare_json_element -> JSONType(hare_json_element) = 'String' "
    "AND JSONExtractString(hare_json_element) = hare_json_key, JSONExtractArrayRaw({document})), "
    "JSONType({document}) = 'String', JSONExtractString({document}) = hare_json_key, 0)"
)

#: Every or any key of an array held by a JSON text document - NULL for a NULL document.
CLICKHOUSE_JSON_KEY_EXISTENCE_SQL = (
    "if({document} IS NULL, NULL, {array_function}(hare_json_key -> {has_key}, {keys}))"
)

#: A JSON value's text as ClickHouse writes it - numbers, escapes and spaces the same whatever text
#: gave the value: the text a path of a stored document reads (``JSONExtractRaw``), so the two compare
#: as equal JSON values.
CLICKHOUSE_JSON_VALUE_TEXT_SQL = "JSONExtractRaw(concat('[', {value}, ']'), 1)"
#: The name of that term - no function of ClickHouse; the term writes its own SQL.
CLICKHOUSE_JSON_VALUE_TEXT_FUNCTION_NAME = "HARE_JSON_VALUE_TEXT"

#: The key a JSON value's text is ordered by, as PostgreSQL orders ``jsonb``: a null, then strings,
#: numbers, booleans, arrays and objects; a number by its value (0 for any other value - a NULL in a
#: tuple makes its comparison NULL), a string by its own text.
CLICKHOUSE_JSON_SORT_KEY_SQL = (
    "tuple(multiIf(JSONType({value}) = 'Null', 0, JSONType({value}) = 'String', 1, "
    "JSONType({value}) IN ('Int64', 'UInt64', 'Double'), 2, JSONType({value}) = 'Bool', 3, "
    "JSONType({value}) = 'Array', 4, 5), ifNull(toFloat64OrNull({value}), 0), "
    "if(JSONType({value}) = 'String', JSONExtractString({value}), {value}))"
)
#: The name of that term.
CLICKHOUSE_JSON_SORT_KEY_FUNCTION_NAME = "HARE_JSON_SORT_KEY"

#: The name of the containment test - no function of ClickHouse; the term writes its own SQL.
CLICKHOUSE_JSON_CONTAINMENT_FUNCTION_NAME = "HARE_JSON_CONTAINMENT"
#: Whether the JSON text ``{document}`` is the same scalar as the bound JSON text ``{scalar}`` - both
#: written as ClickHouse writes a JSON value (``1.0`` as ``1``, ``\/`` as ``/``), so their texts compare.
CLICKHOUSE_JSON_SAME_SCALAR_SQL = (
    "JSONExtractRaw(concat('[', {document}, ']'), 1) = JSONExtractRaw(concat('[', {scalar}, ']'), 1)"
)

#: The functions naming the type of a ``Dynamic`` (a ``Variant``) value and reading it as a value of
#: a type - NULL (the type's default) for a value of another one.
CLICKHOUSE_DYNAMIC_TYPE_FUNCTION_NAME = "dynamicType"
CLICKHOUSE_DYNAMIC_ELEMENT_FUNCTION_NAME = "dynamicElement"
CLICKHOUSE_VARIANT_TYPE_FUNCTION_NAME = "variantType"
CLICKHOUSE_VARIANT_ELEMENT_FUNCTION_NAME = "variantElement"
#: How a ``Dynamic`` value's decimal type begins, and how one is read to be compared with a decimal of
#: another type - as a ``Decimal256`` of this scale, NULL for a text that isn't one.
CLICKHOUSE_DYNAMIC_DECIMAL_TYPE_PREFIX = "Decimal("
CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_FUNCTION_NAME = "toDecimal256OrNull"
CLICKHOUSE_DYNAMIC_COMPARED_DECIMAL_SCALE = 38
#: The fewest values of an ``__in`` list bound as one parameter - a read sends them as an external table, as
#: the server parses a long list of literals far slower than it reads the same values as data.
CLICKHOUSE_VALUE_SET_MINIMUM = 500
#: The integer types the column of a set of values takes, the narrowest first, each with its range.
CLICKHOUSE_VALUE_SET_INTEGER_TYPES = (
    ("Int64", -(2**63), 2**63 - 1),
    ("UInt64", 0, 2**64 - 1),
    ("Int128", -(2**127), 2**127 - 1),
    ("Int256", -(2**255), 2**255 - 1),
)

#: What a JSON value is written between to be the member of an object - ClickHouse casts only an object to
#: ``JSON``, and the canonical text of any JSON value is read from that object's.
CLICKHOUSE_JSON_MEMBER_PREFIX = '{"v":'
CLICKHOUSE_JSON_MEMBER_SUFFIX = "}"
#: The type a JSON text is cast to for its canonical text - NULL stays NULL.
CLICKHOUSE_JSON_TYPE_NAME = "Nullable(JSON)"

#: The text of a value - a UUID's orders as its bytes do.
CLICKHOUSE_TEXT_FUNCTION_NAME = "toString"
#: A UUID of a text or a UUID.
CLICKHOUSE_TO_UUID_FUNCTION_NAME = "toUUID"

#: The offset of a time of day's text (``+03:00``), '' for a time without one.
CLICKHOUSE_TIME_OFFSET_TEXT_SQL = "extract({value}, '([+-][0-9]{{2}}:[0-9]{{2}}(:[0-9]{{2}})?)$')"
#: The offset of a time of day's text in seconds, 0 for a time without one.
CLICKHOUSE_TIME_OFFSET_SECONDS_SQL = (
    "if({offset} = '', 0, if(substring({offset}, 1, 1) = '-', -1, 1) * "
    "(toInt64OrZero(substring({offset}, 2, 2)) * 3600 + toInt64OrZero(substring({offset}, 5, 2)) * 60 + "
    "toInt64OrZero(substring({offset}, 8, 2))))"
)
#: The wall clock of a time of day's text in microseconds.
CLICKHOUSE_TIME_WALL_CLOCK_SQL = (
    "(toInt64OrZero(substring({value}, 1, 2)) * 3600000000 + toInt64OrZero(substring({value}, 4, 2)) * 60000000 + "
    "toInt64OrZero(substring({value}, 7, 2)) * 1000000 + if(substring({value}, 9, 1) = '.', "
    "toInt64OrZero(rightPad(extract({value}, '^[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}[.]([0-9]{{1,6}})'), 6, '0')), 0))"
)
#: The key a time of day's text orders by, as PostgreSQL orders ``TIMETZ``: by the UTC time in
#: microseconds, then by the offset, the larger one first - one integer, NULL for NULL.
CLICKHOUSE_TIME_SORT_KEY_SQL = "(({wall_clock} - {offset_seconds} * 1000000) * 1000000 + (86400 - {offset_seconds}))"
#: The name of the function the key is written as.
CLICKHOUSE_TIME_SORT_KEY_FUNCTION_NAME = "HARE_TIME_SORT_KEY"

#: An IPv6 value of a text or an IPv4 value.
CLICKHOUSE_TO_IPV6_FUNCTION_NAME = "toIPv6"
#: The hex of the first 12 bytes of an IPv4 address held as an IPv6 one (``::ffff:a.b.c.d``).
CLICKHOUSE_IPV4_MAPPED_HEX_PREFIX = "00000000000000000000FFFF"
#: The key an ``IPv6`` value orders by, as PostgreSQL orders ``inet`` - its family (4 before 6), then its
#: bytes in hex; NULL for NULL.
CLICKHOUSE_ADDRESS_SORT_KEY_SQL = (
    "concat(if(startsWith(hex({value}), '" + CLICKHOUSE_IPV4_MAPPED_HEX_PREFIX + "'), '4', '6'), hex({value}))"
)
#: The name of the function the key is written as.
CLICKHOUSE_ADDRESS_SORT_KEY_FUNCTION_NAME = "HARE_ADDRESS_SORT_KEY"
#: The text of an ``IPv6`` value - an IPv4 address held as ``::ffff:a.b.c.d`` written as ``a.b.c.d``.
CLICKHOUSE_ADDRESS_TEXT_SQL = (
    "if(startsWith(toString({value}), '::ffff:') AND position(toString({value}), '.') > 0, "
    "substring(toString({value}), 8), toString({value}))"
)
#: The name of the key an array orders by - each element by its own key.
CLICKHOUSE_ARRAY_ELEMENTS_KEY_FUNCTION_NAME = "HARE_ARRAY_ELEMENTS_KEY"
#: The key an array orders by: ``arrayMap`` of the key of each element, named by the variable.
CLICKHOUSE_ARRAY_ELEMENTS_KEY_SQL = "arrayMap({variable} -> {element_key}, {value})"
#: The name of the lambda variable an element of an array is keyed by, by the depth of the array.
CLICKHOUSE_ARRAY_ELEMENT_VARIABLE = "hare_element_{depth}"
#: The function reading an element of a tuple by its position.
CLICKHOUSE_TUPLE_ELEMENT_FUNCTION_NAME = "tupleElement"
#: The function building a tuple.
CLICKHOUSE_TUPLE_FUNCTION_NAME = "tuple"
