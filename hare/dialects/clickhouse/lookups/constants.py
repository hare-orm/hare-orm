from __future__ import annotations

#: The name of the key existence test - no function of ClickHouse; the term writes its own SQL.
CLICKHOUSE_JSON_KEY_EXISTENCE_FUNCTION_NAME = "HARE_JSON_KEY_EXISTENCE"

#: Whether the JSON text ``{document}`` holds the key ``hare_json_key``. The lambda parameter is named
#: as no column is, which it would hide.
CLICKHOUSE_JSON_HAS_KEY_SQL = "JSONHas({document}, hare_json_key)"

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

#: The typed value ClickHouse reads of a JSON value's text - ordered and compared as ClickHouse orders
#: and compares a ``Dynamic``; NULL for a JSON null and for NULL, read as the JSON null (a ``Dynamic`` is
#: never ``Nullable``).
CLICKHOUSE_JSON_TYPED_VALUE_SQL = "JSONExtract(ifNull(toString({value}), 'null'), 'Dynamic')"

#: The type a compared JSON text is cast to on a ``JSON`` column - NULL stays NULL.
CLICKHOUSE_JSON_TYPE_NAME = "Nullable(JSON)"

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
