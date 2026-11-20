"""Selected query cells of the generated-field matrix; reading covers every field sample."""

GENERATED_VALUE_OPERATIONS = frozenset({"json", "values", "annotation"})

# SQL JSON construction has its own forms, independent of the field's outbox serializer.
# PostgreSQL bytes and SQLite booleans have explicit profile adjustments in the oracle.
GENERATED_JSON_OBJECT_VALUES = {
    "int": (5, None),
    "float": (1.5, -0.25),
    "decimal": (12.5,),
    "boolean": (True, False),
    "char": ("a b",),
    "binary": ("0068617265",),
    "json": ({"a": [1, 2.5, None, "x"]},),
    "json_text": ({"a": [1, None]},),
    "uuid": ("12345678-1234-5678-1234-567812345678",),
}

GENERATED_INTEGER_SAMPLES = frozenset(
    {
        "int",
        "small_int",
        "big_int",
        "positive_int",
        "positive_small_int",
        "positive_big_int",
        "int_enum",
        "clickhouse_integer",
        "int8",
        "int128",
        "int256",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uint128",
        "uint256",
    }
)
GENERATED_NUMERIC_SAMPLES = GENERATED_INTEGER_SAMPLES | {"float", "float32", "decimal"}
GENERATED_ORDERED_SAMPLES = GENERATED_NUMERIC_SAMPLES | {
    "boolean",
    "char",
    "text",
    "email",
    "url",
    "slug",
    "phone",
    "phone_region",
    "date",
    "datetime",
    "time",
    "time_delta",
    "char_enum",
    "citext",
    "fixed_char",
    "interval",
    "naive_datetime",
    "naive_time",
    "native_enum",
    "clickhouse_date",
    "clickhouse_datetime",
}

# These are selected contracts, not a copy of the production type registry. Fractions
# exercise integer rejection and numeric promotion; compound values retain their own
# read contracts and independent container/GIS/JSON operation matrices.
GENERATED_OPERATION_SAMPLES = {
    "json_object": frozenset({"int", "float", "decimal", "boolean", "char", "binary", "json", "json_text", "uuid"}),
    "ordering": GENERATED_ORDERED_SAMPLES,
    "annotation_ordering": GENERATED_ORDERED_SAMPLES,
    "filter": GENERATED_ORDERED_SAMPLES | {"uuid", "binary", "array_of_uuids", "array_of_bytes", "array_non_nullable"},
    "distinct": GENERATED_ORDERED_SAMPLES
    | {"uuid", "binary", "array_of_uuids", "array_of_bytes", "array_non_nullable"},
    "aggregate": GENERATED_ORDERED_SAMPLES,
    "aggregate_empty": GENERATED_ORDERED_SAMPLES,
    "greatest_least": GENERATED_NUMERIC_SAMPLES | {"char", "text"},
    "coalesce_fractional": GENERATED_NUMERIC_SAMPLES | {"boolean", "char"},
    "lag_fractional": GENERATED_NUMERIC_SAMPLES,
}
