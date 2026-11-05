"""Catalog declarations, storage choices and driver codecs agree on physical types."""

import sqlite3
from decimal import Decimal, localcontext

import pytest

from hare import fields
from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues
from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.postgresql.postgresql_introspector import PostgresqlIntrospector
from hare.dialects.postgresql.schema.columns.postgresql_column_type_changes import PostgresqlColumnTypeChanges
from hare.dialects.postgresql.types.postgresql_physical_type import PostgresqlPhysicalType
from hare.dialects.postgresql.types.postgresql_types import PostgresqlTypes
from hare.dialects.sqlite.types.sqlite_physical_type import SqlitePhysicalType
from hare.sql.functions.cast import Cast

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("type_sql", ['"paint".status', '"Mixed"."Status"', '"paint".status[]'])
def test_casts_preserve_quoted_type_names(type_sql):
    dialect = PostgresqlDialect()
    assert Cast(1, type_sql).get_sql(dialect.sql_context) == f"CAST(1 AS {type_sql})"
    assert dialect.clauses.get_typed_placeholder_template("$1", type_sql) == f"CAST($1 AS {type_sql})"


@pytest.mark.parametrize("field", [fields.IntField(), fields.CharField(max_length=20), fields.DecimalField(10, 2)])
@pytest.mark.parametrize("array", [False, True])
def test_postgresql_array_typmods_do_not_require_scalar_index_extensions(field, array):
    assert PostgresqlTypes.is_plain_scalar_field(fields.ArrayField(field) if array else field) is not array


@pytest.mark.parametrize(
    ("declaration", "canonical", "parameters"),
    [
        ("VARCHAR(50)", "character varying(50)", ("50",)),
        ("TIMESTAMP(3) WITH TIME ZONE", "timestamp(3) with time zone", ("3",)),
        ("decimal(12, -3) [] []", "numeric(12,-3)[]", ("12", "-3")),
        ("CHAR", "character(1)", None),
        ("integer[][]", "integer[]", None),
        ('"Schema"."Name(20)"[]', None, None),
    ],
)
def test_postgresql_declaration_metadata(declaration, canonical, parameters):
    physical = PostgresqlPhysicalType.from_sql(declaration)
    assert physical.canonical_sql == canonical
    assert physical.parameters == parameters
    assert PostgresqlIntrospector.get_canonical_type(declaration) == canonical
    if canonical is not None:
        assert not PostgresqlColumnTypeChanges.column_type_change_rewrites_table(declaration, canonical)
    else:
        assert physical.element.sql == '"Schema"."Name(20)"'
        assert physical.element.without_modifiers == '"Schema"."Name(20)"'


@pytest.mark.parametrize(
    ("old", "new", "rewrite"),
    [
        ("VARCHAR(5)", "VARCHAR(10)", False),
        ("VARCHAR(10)", "VARCHAR(5)", True),
        ("numeric(5,2)", "decimal(8,2)", False),
        ("numeric(5,2)", "numeric(5,3)", True),
        ('"Type"', '"type"', True),
    ],
)
def test_postgresql_rewrite_uses_known_type_properties(old, new, rewrite):
    assert PostgresqlColumnTypeChanges.column_type_change_rewrites_table(old, new) is rewrite


@pytest.mark.parametrize(
    ("declaration", "value"),
    [
        ("Array(Map(String, Nullable(Decimal(18, 4))))", [{"key": Decimal("12.3400")}, {"other": None}]),
        ('Tuple(`name with space` String, "quote" Array(Int32))', ("hello", [1, -2])),
        (r"Enum8('a=b' = 1, 'a''b' = 2, 'a\\b' = 3)", "a'b"),
    ],
)
def test_clickhouse_catalog_and_binary_type_roundtrip(declaration, value):
    encoded_type = bytearray()
    ClickhouseSharedVariantValues.write_type(declaration, encoded_type)
    decoded_type, offset = ClickhouseSharedVariantValues.read_type(bytes(encoded_type), 0)
    assert offset == len(encoded_type)
    rewritten_type = bytearray()
    ClickhouseSharedVariantValues.write_type(decoded_type, rewritten_type)
    assert rewritten_type == encoded_type
    if declaration.startswith("Enum8"):
        # Wire fixture: tag, member count, UTF-8 labels with lengths, signed member numbers.
        assert encoded_type == b"\x17\x03\x03a=b\x01\x03a'b\x02\x03a\\b\x03"
    blob = bytearray()
    ClickhouseSharedVariantValues.write_value(value, declaration, blob)
    actual, offset = ClickhouseSharedVariantValues.read_value(decoded_type, bytes(blob), 0)
    assert (actual, offset) == (value, len(blob))
    specification = ClickhouseTypeParser.get_specification(decoded_type)
    assert ClickhouseTypeParser.holds_only_mapped_types(specification)
    if declaration.startswith("Tuple"):
        assert tuple(specification.kwargs["element_fields"]) == ("name with space", "quote")


@pytest.mark.parametrize("element", ["String", "FutureType(42)"])
@pytest.mark.parametrize(
    "wrapper",
    ["{}", "Nullable({})", "LowCardinality(Nullable({}))", "Array(Nullable({}))", "Map(String, Array(Nullable({})))"],
)
def test_clickhouse_unknown_type_evidence_survives_wrappers(element, wrapper):
    specification = ClickhouseTypeParser.get_specification(wrapper.format(element))
    assert ClickhouseTypeParser.holds_only_mapped_types(specification) is (element == "String")


@pytest.mark.parametrize("precision", [6, 28, 76])
@pytest.mark.parametrize("negative", [False, True])
def test_clickhouse_decimal_wire_values_ignore_python_context(precision, negative):
    coefficient = 12345678901234567890123456789012
    text = "123456789012345678901234567890.12"
    if negative:
        coefficient = -coefficient
        text = "-" + text
    value = Decimal(text)
    expected = coefficient.to_bytes(16, "little", signed=True)
    with localcontext() as context:
        context.prec = precision
        context.clear_flags()
        blob = bytearray()
        ClickhouseSharedVariantValues.write_value(value, "Decimal(32, 2)", blob)
        assert blob == expected
        actual, offset = ClickhouseSharedVariantValues.read_value("Decimal(32, 2)", expected, 0)
        assert (actual.as_tuple(), offset) == (value.as_tuple(), len(expected))
        assert not any(context.flags.values())


def test_missing_type_properties_remain_unknown():
    assert ClickhousePhysicalType.from_sql("DateTime64(9)").timezone is None
    assert PostgresqlPhysicalType.from_sql("numeric").numeric_precision_scale is None
    assert PostgresqlPhysicalType.from_sql("character").character_length is None
    unknown = ClickhousePhysicalType.from_sql("Array(FutureType(42, 'opaque'))").elements[0]
    assert unknown.sql == "FutureType(42, 'opaque')"
    assert unknown.elements == ()


@pytest.mark.parametrize("declaration", ["", "BLOB", "VARCHAR(30)", "FLOATING POINT", "STRING", "REAL"])
def test_sqlite_affinity_agrees_with_actual_storage(declaration):
    expected_storage = {"BLOB": "text", "TEXT": "text", "INTEGER": "integer", "NUMERIC": "integer", "REAL": "real"}
    physical = SqlitePhysicalType.from_sql(declaration)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"CREATE TABLE value_storage (value {declaration})")
        connection.execute("INSERT INTO value_storage VALUES (?)", ("5",))
        actual = connection.execute("SELECT typeof(value) FROM value_storage").fetchone()[0]
        assert actual == expected_storage[physical.affinity]
    finally:
        connection.close()
