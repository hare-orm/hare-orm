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
#: numbers, booleans, arrays and objects; a number by its value, a string by its own text.
CLICKHOUSE_JSON_SORT_KEY_SQL = (
    "tuple(multiIf(JSONType({value}) = 'Null', 0, JSONType({value}) = 'String', 1, "
    "JSONType({value}) IN ('Int64', 'UInt64', 'Double'), 2, JSONType({value}) = 'Bool', 3, "
    "JSONType({value}) = 'Array', 4, 5), toFloat64OrNull({value}), "
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
