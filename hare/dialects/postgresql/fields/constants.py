from __future__ import annotations

import re

from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.query.enums import Lookup

#: The name a ``NativeEnumField``'s ``ENUM`` type may take - a lowercase identifier written without
#: quotes.
NATIVE_ENUM_TYPE_NAME_PATTERN = re.compile(r"[a-z_][a-z0-9_]*")
#: The longest such name - PostgreSQL's NAMEDATALEN - 1.
NATIVE_ENUM_TYPE_NAME_MAX_LENGTH = 63
#: The longest label of an ``ENUM`` type, in bytes.
NATIVE_ENUM_LABEL_MAX_BYTES = 63
#: The column types of the network fields.
POSTGRESQL_INET_TYPE = "inet"
POSTGRESQL_CIDR_TYPE = "cidr"
POSTGRESQL_MACADDR_TYPE = "macaddr"
#: The functions a network field's path segment reads - ``ip__family`` (4 or 6), ``ip__masklen``.
POSTGRESQL_NETWORK_PATH_FUNCTIONS = {"family": "family", "masklen": "masklen"}
#: The separators a MAC address may be written with - ``08:00:2b:01:02:03``, ``08-00-2b-01-02-03``,
#: ``0800.2b01.0203``, ``08002b010203``.
MAC_ADDRESS_SEPARATOR_PATTERN = re.compile(r"[:\-.]")
#: A MAC address without its separators - twelve hex digits.
MAC_ADDRESS_DIGITS_PATTERN = re.compile(r"[0-9a-fA-F]{12}")
#: The column type of ``LtreeField`` and the types of its query values.
POSTGRESQL_LTREE_TYPE = "ltree"
POSTGRESQL_LQUERY_TYPE = "lquery"
POSTGRESQL_LTXTQUERY_TYPE = "ltxtquery"
#: The separator of an ltree path's labels.
LTREE_LABEL_SEPARATOR = "."
#: One label of an ltree path - letters, digits, underscores and (PostgreSQL 16+) hyphens.
LTREE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")
#: The functions an ltree field's path segment reads - ``path__depth``, the number of labels.
POSTGRESQL_LTREE_PATH_FUNCTIONS = {"depth": "nlevel"}
#: Splits a class name into its words for the default type name - ``OrderStatus`` -> ``order_status``.
CLASS_NAME_WORD_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Range path segments reading a bound, by the PostgreSQL function giving it.
RANGE_BOUND_PATH_FUNCTIONS = {"startswith": "lower", "endswith": "upper"}

#: Range path segments reading a boolean property, each the PostgreSQL function of the same name.
RANGE_FLAG_PATH_FUNCTIONS = frozenset({"isempty", "lower_inc", "lower_inf", "upper_inc", "upper_inf"})

#: The multirange path segment reading the smallest range holding every member, and its function.
MULTIRANGE_SPAN_PATH_SEGMENT = "span"

MULTIRANGE_SPAN_PATH_FUNCTION = "range_merge"

#: The methods a range field reads a value through - a subclass overriding one reads it in Python.
RANGE_READING_METHOD_NAMES = (
    "from_db_value",
    "to_python",
    "get_range",
    "canonicalize",
    "coerce_bound",
    "get_python_bound",
    "get_python_range",
    "get_infinite_bound",
)

#: hstore path segments reading every key or value as a text array, by the PostgreSQL function.
HSTORE_ARRAY_PATH_FUNCTIONS = {"keys": "akeys", "values": "avals"}

#: Lookups of a range field whose filter value is a range - ``contains`` also takes one bound value.
RANGE_VALUE_LOOKUPS = frozenset(
    {
        Lookup.EXACT,
        Lookup.NOT,
        Lookup.CONTAINED_BY,
        Lookup.OVERLAP,
        PostgresqlLookup.FULLY_LT,
        PostgresqlLookup.FULLY_GT,
        PostgresqlLookup.NOT_LT,
        PostgresqlLookup.NOT_GT,
        PostgresqlLookup.ADJACENT_TO,
    }
)

#: The module of each field the package exports - imported on first use: the dialect, registering
#: what it adds to one of them, doesn't load them all.
EXPORTED_MODULES = {
    "BigIntMultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "BigIntRangeField": "hare.dialects.postgresql.fields.ranges",
    "CidrField": "hare.dialects.postgresql.fields.network",
    "CitextField": "hare.dialects.postgresql.fields.citext_field",
    "DateMultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "DateRangeField": "hare.dialects.postgresql.fields.ranges",
    "DateTimeMultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "DateTimeRangeField": "hare.dialects.postgresql.fields.ranges",
    "DecimalMultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "DecimalRangeField": "hare.dialects.postgresql.fields.ranges",
    "HStoreField": "hare.dialects.postgresql.fields.hstore",
    "InetField": "hare.dialects.postgresql.fields.network",
    "IntMultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "IntRangeField": "hare.dialects.postgresql.fields.ranges",
    "LtreeField": "hare.dialects.postgresql.fields.ltree_field",
    "MacAddressField": "hare.dialects.postgresql.fields.network",
    "MultiRangeField": "hare.dialects.postgresql.fields.multiranges",
    "NativeEnumField": "hare.dialects.postgresql.fields.native_enum",
    "PostGISField": "hare.dialects.postgresql.fields.postgis_field",
    "Range": "hare.dialects.postgresql.fields.ranges",
    "RangeField": "hare.dialects.postgresql.fields.ranges",
    "TSVectorField": "hare.dialects.postgresql.fields.ts_vector_field",
}
