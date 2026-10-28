"""The samples of hare's field classes - one or more of each class a model declares (``FieldSample``), with
values of it and the JSON form of each: what an outbox event holds of it."""

from __future__ import annotations

import datetime
import importlib
import ipaddress
import uuid
from decimal import Decimal

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse import fields as clickhouse_fields
from hare.dialects.postgresql import fields as postgresql_fields
from hare.dialects.postgresql.fields.ranges import Range
from hare.fields.data.json.json_text_field import JSONTextField
from hare.gis import (
    GeometryCollectionField,
    GeometryField,
    LineString,
    LineStringField,
    MultiLineString,
    MultiLineStringField,
    MultiPoint,
    MultiPointField,
    MultiPolygon,
    MultiPolygonField,
    Point,
    PointField,
    Polygon,
    PolygonField,
)
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.vectors import VectorField
from tests.contracts.constants import PYDANTIC_DOCUMENT_MODULE
from tests.contracts.enums import SampleColor, SampleLevel
from tests.contracts.field_sample import FieldSample

#: The relation target of the relation samples.
TARGET_MODEL = "contracts.ContractTarget"
SAMPLE_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
SAMPLE_MOMENT = datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.UTC)
SAMPLE_NEXT_MOMENT = SAMPLE_MOMENT + datetime.timedelta(days=1)
SQUARE = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))


def get_range_form(lower, upper, lower_inc=True, upper_inc=False, is_empty=False):
    """The JSON form of a range - the mapping RangeField reads back."""
    return {"lower": lower, "upper": upper, "lower_inc": lower_inc, "upper_inc": upper_inc, "is_empty": is_empty}


FIELD_SAMPLES = (
    # Core fields.
    FieldSample("int", lambda: {"value": fields.IntField(null=True)}, (5, None), (5, None)),
    FieldSample("small_int", lambda: {"value": fields.SmallIntField(null=True)}, (-3,), (-3,)),
    FieldSample("big_int", lambda: {"value": fields.BigIntField(null=True)}, (2**40,), (2**40,)),
    FieldSample("positive_int", lambda: {"value": fields.PositiveIntField(null=True)}, (7,), (7,)),
    FieldSample("positive_small_int", lambda: {"value": fields.PositiveSmallIntField(null=True)}, (7,), (7,)),
    FieldSample("positive_big_int", lambda: {"value": fields.PositiveBigIntField(null=True)}, (2**40,), (2**40,)),
    FieldSample("float", lambda: {"value": fields.FloatField(null=True)}, (1.5, -0.25), (1.5, -0.25)),
    FieldSample(
        "float_non_finite",
        lambda: {"value": fields.FloatField(null=True)},
        (float("nan"), float("inf"), float("-inf")),
        ("NaN", "Infinity", "-Infinity"),
        value_dialects=("postgresql", "clickhouse"),
        request_filtered=False,
    ),
    FieldSample(
        "decimal",
        lambda: {"value": fields.DecimalField(max_digits=10, decimal_places=2, null=True)},
        (Decimal("12.50"),),
        ("12.50",),
    ),
    FieldSample("boolean", lambda: {"value": fields.BooleanField(null=True)}, (True, False), (True, False)),
    FieldSample("binary", lambda: {"value": fields.BinaryField(null=True)}, (b"\x00hare",), ("AGhhcmU=",)),
    FieldSample("char", lambda: {"value": fields.CharField(max_length=20, null=True)}, ("a b",), ("a b",)),
    FieldSample("text", lambda: {"value": fields.TextField(null=True)}, ("a\nb",), ("a\nb",)),
    FieldSample("email", lambda: {"value": fields.EmailField(null=True)}, ("ann@example.com",), ("ann@example.com",)),
    FieldSample(
        "url", lambda: {"value": fields.URLField(null=True)}, ("https://example.com/x",), ("https://example.com/x",)
    ),
    FieldSample("slug", lambda: {"value": fields.SlugField(null=True)}, ("a-b",), ("a-b",)),
    FieldSample("phone", lambda: {"value": fields.PhoneField(null=True)}, ("+16502530000",), ("+16502530000",)),
    FieldSample(
        "phone_region",
        lambda: {"value": fields.PhoneField(region="US", null=True)},
        ("+16502530000",),
        ("+16502530000",),
        requires=("phone",),
    ),
    FieldSample("uuid", lambda: {"value": fields.UUIDField(null=True)}, (SAMPLE_UUID,), (str(SAMPLE_UUID),)),
    FieldSample(
        "date", lambda: {"value": fields.DateField(null=True)}, (datetime.date(2024, 1, 15),), ("2024-01-15",)
    ),
    FieldSample(
        "datetime",
        lambda: {"value": fields.DatetimeField(null=True)},
        (SAMPLE_MOMENT,),
        ("2024-01-15T10:30:00+00:00",),
    ),
    FieldSample("time", lambda: {"value": fields.TimeField(null=True)}, (datetime.time(10, 30),), ("10:30:00+00:00",)),
    FieldSample(
        "time_delta",
        lambda: {"value": fields.TimeDeltaField(null=True)},
        (datetime.timedelta(seconds=90),),
        (90.0,),
    ),
    FieldSample(
        "char_enum", lambda: {"value": fields.CharEnumField(SampleColor, null=True)}, (SampleColor.RED,), ("red",)
    ),
    FieldSample("int_enum", lambda: {"value": fields.IntEnumField(SampleLevel, null=True)}, (SampleLevel.HIGH,), (2,)),
    FieldSample(
        "ip_address",
        lambda: {"value": fields.IPAddressField(null=True)},
        (ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("::1")),
        ("10.0.0.1", "::1"),
        request_values=("10.0.0.1", "::1"),
    ),
    FieldSample(
        "ipv4_address",
        lambda: {"value": fields.IPv4AddressField(null=True)},
        (ipaddress.ip_address("10.0.0.1"),),
        ("10.0.0.1",),
        request_values=("10.0.0.1",),
    ),
    FieldSample(
        "json",
        lambda: {"value": fields.JSONField(null=True)},
        ({"a": [1, 2.5, None, "x"]},),
        ({"a": [1, 2.5, None, "x"]},),
        request_filter_refused=True,
    ),
    FieldSample(
        "json_list",
        lambda: {"value": fields.JSONField(null=True)},
        ([1, {"b": True}],),
        ([1, {"b": True}],),
        value_dialects=("sqlite", "postgresql"),
        request_filter_refused=True,
    ),
    FieldSample(
        "json_text",
        lambda: {"value": JSONTextField(null=True)},
        ({"a": [1, None]},),
        ({"a": [1, None]},),
        request_filter_refused=True,
    ),
    FieldSample(
        "json_pydantic",
        lambda: {
            "value": fields.JSONField(
                field_type=importlib.import_module(PYDANTIC_DOCUMENT_MODULE).PydanticDocument, null=True
            )
        },
        ({"title": "a", "count": 2},),
        ({"title": "a", "count": 2},),
        requires=("pydantic",),
    ),
    FieldSample("array", lambda: {"value": fields.ArrayField(fields.IntField(), null=True)}, ([1, 2],), ([1, 2],)),
    FieldSample(
        "array_of_floats",
        lambda: {"value": fields.ArrayField(fields.FloatField(), null=True)},
        ([1.5, float("nan"), float("inf")],),
        ([1.5, "NaN", "Infinity"],),
        value_dialects=("postgresql", "clickhouse"),
    ),
    FieldSample(
        "tuple",
        lambda: {"value": fields.TupleField([fields.IntField(), fields.CharField(max_length=5)])},
        ((1, "ab"),),
        ([1, "ab"],),
    ),
    FieldSample(
        "tuple_named",
        lambda: {
            "value": fields.TupleField({"longitude": fields.FloatField(), "latitude": fields.FloatField()}),
        },
        ({"longitude": 2.5, "latitude": 48.5},),
        ({"longitude": 2.5, "latitude": 48.5},),
    ),
    FieldSample(
        "map",
        lambda: {"value": fields.MapField(fields.CharField(max_length=10), fields.IntField())},
        ({"a": 1, "b": 2},),
        ([["a", 1], ["b", 2]],),
    ),
    FieldSample(
        "map_of_uuid_keys",
        lambda: {"value": fields.MapField(fields.UUIDField(), fields.IntField())},
        ({SAMPLE_UUID: 1},),
        ([[str(SAMPLE_UUID), 1]],),
    ),
    FieldSample(
        "nested",
        lambda: {"value": fields.NestedField({"sku": fields.CharField(max_length=10), "quantity": fields.IntField()})},
        ([{"sku": "a-1", "quantity": 2}],),
        ([{"sku": "a-1", "quantity": 2}],),
        request_values=([("a-1", 2)],),
    ),
    FieldSample(
        "generated",
        lambda: {
            "base": fields.IntField(default=1),
            "value": fields.GeneratedField(expression=RawSQLTerm("base + 1"), output_field=fields.IntField()),
        },
    ),
    FieldSample(
        "encrypted_text",
        lambda: {"value": fields.EncryptedTextField(null=True)},
        ("secret",),
        requires=("encryption",),
        captured=False,
    ),
    FieldSample(
        "encrypted_json",
        lambda: {"value": fields.EncryptedJSONField(null=True)},
        ({"a": "secret"},),
        requires=("encryption",),
        captured=False,
    ),
    FieldSample(
        "blind_index",
        lambda: {"secret": fields.EncryptedTextField(null=True), "value": fields.BlindIndexField("secret")},
        requires=("encryption",),
        captured=False,
    ),
    # Relations - their values are the target's keys.
    FieldSample(
        "foreign_key",
        lambda: {"value": fields.ForeignKeyField(TARGET_MODEL, null=True)},
        (1,),
        (1,),
        related=True,
        column_name="value_id",
    ),
    FieldSample(
        "one_to_one",
        lambda: {"value": fields.OneToOneField(TARGET_MODEL, null=True)},
        (1,),
        (1,),
        related=True,
        column_name="value_id",
    ),
    FieldSample("many_to_many", lambda: {"value": fields.ManyToManyField(TARGET_MODEL)}, related=True),
    FieldSample(
        "generic_foreign_key",
        lambda: {"value": fields.GenericForeignKeyField({"contract_target": TARGET_MODEL}, null=True)},
        (1,),
        (1,),
        related=True,
        column_name="contract_target_id",
        registered_live=False,
    ),
    # PostgreSQL's fields.
    FieldSample("citext", lambda: {"value": postgresql_fields.CitextField(null=True)}, ("Ann",), ("Ann",)),
    FieldSample(
        "fixed_char",
        lambda: {"value": postgresql_fields.FixedCharField(max_length=5, null=True)},
        ("abcde",),
        ("abcde",),
    ),
    FieldSample(
        "hstore",
        lambda: {"value": postgresql_fields.HStoreField(null=True)},
        ({"a": "1", "b": None},),
        ({"a": "1", "b": None},),
    ),
    FieldSample("ltree", lambda: {"value": postgresql_fields.LtreeField(null=True)}, ("a.b.c",), ("a.b.c",)),
    FieldSample(
        "ts_vector", lambda: {"value": postgresql_fields.TSVectorField(null=True)}, ("'a' 'cat'",), ("'a' 'cat'",)
    ),
    FieldSample(
        "interval",
        lambda: {"value": postgresql_fields.IntervalField(null=True)},
        (datetime.timedelta(days=1, seconds=5),),
        (86405.0,),
    ),
    FieldSample(
        "inet",
        lambda: {"value": postgresql_fields.InetField(null=True)},
        (ipaddress.ip_interface("10.0.0.1/24"),),
        ("10.0.0.1/24",),
        request_values=("10.0.0.1/24",),
    ),
    FieldSample(
        "cidr",
        lambda: {"value": postgresql_fields.CidrField(null=True)},
        (ipaddress.ip_network("10.0.0.0/8"),),
        ("10.0.0.0/8",),
        request_values=("10.0.0.0/8",),
    ),
    FieldSample(
        "mac_address",
        lambda: {"value": postgresql_fields.MacAddressField(null=True)},
        ("08:00:2b:01:02:03",),
        ("08:00:2b:01:02:03",),
    ),
    FieldSample(
        "naive_datetime",
        lambda: {"value": postgresql_fields.NaiveDatetimeField(null=True)},
        (datetime.datetime(2024, 1, 15, 10, 30),),
        ("2024-01-15T10:30:00",),
    ),
    FieldSample(
        "naive_time",
        lambda: {"value": postgresql_fields.NaiveTimeField(null=True)},
        (datetime.time(10, 30),),
        ("10:30:00",),
    ),
    FieldSample(
        "native_enum",
        lambda: {"value": postgresql_fields.NativeEnumField(SampleColor, type_name="contract_color", null=True)},
        (SampleColor.BLUE,),
        ("blue",),
    ),
    FieldSample(
        "postgis", lambda: {"value": postgresql_fields.PostGISField(null=True)}, ((55.75, 37.62),), ([55.75, 37.62],)
    ),
    FieldSample(
        "int_range",
        lambda: {"value": postgresql_fields.IntRangeField(null=True)},
        (Range(1, 5),),
        (get_range_form(1, 5),),
        request_values=((1, 5),),
    ),
    FieldSample(
        "big_int_range",
        lambda: {"value": postgresql_fields.BigIntRangeField(null=True)},
        (Range(1, 2**40),),
        (get_range_form(1, 2**40),),
        request_values=((1, 2**40),),
    ),
    FieldSample(
        "decimal_range",
        lambda: {"value": postgresql_fields.DecimalRangeField(null=True)},
        (Range(Decimal("1.5"), Decimal("2.5")),),
        (get_range_form("1.5", "2.5"),),
        request_values=((Decimal("1.5"), Decimal("2.5")),),
    ),
    FieldSample(
        "date_range",
        lambda: {"value": postgresql_fields.DateRangeField(null=True)},
        (Range(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),),
        (get_range_form("2024-01-01", "2024-02-01"),),
        request_values=((datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),),
    ),
    FieldSample(
        "date_time_range",
        lambda: {"value": postgresql_fields.DateTimeRangeField(null=True)},
        (Range(SAMPLE_MOMENT, SAMPLE_NEXT_MOMENT),),
        (get_range_form("2024-01-15T10:30:00+00:00", "2024-01-16T10:30:00+00:00"),),
        request_values=((SAMPLE_MOMENT, SAMPLE_NEXT_MOMENT),),
    ),
    FieldSample(
        "int_multi_range",
        lambda: {"value": postgresql_fields.IntMultiRangeField(null=True)},
        ([Range(1, 3), Range(5, 8)],),
        ([get_range_form(1, 3), get_range_form(5, 8)],),
        request_filtered=False,
    ),
    FieldSample(
        "big_int_multi_range",
        lambda: {"value": postgresql_fields.BigIntMultiRangeField(null=True)},
        ([Range(1, 3)],),
        ([get_range_form(1, 3)],),
        request_values=((1, 3),),
    ),
    FieldSample(
        "decimal_multi_range",
        lambda: {"value": postgresql_fields.DecimalMultiRangeField(null=True)},
        ([Range(Decimal("1.5"), Decimal("2.5"))],),
        ([get_range_form("1.5", "2.5")],),
        request_values=((Decimal("1.5"), Decimal("2.5")),),
    ),
    FieldSample(
        "date_multi_range",
        lambda: {"value": postgresql_fields.DateMultiRangeField(null=True)},
        ([Range(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))],),
        ([get_range_form("2024-01-01", "2024-02-01")],),
        request_values=((datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),),
    ),
    FieldSample(
        "date_time_multi_range",
        lambda: {"value": postgresql_fields.DateTimeMultiRangeField(null=True)},
        ([Range(SAMPLE_MOMENT, SAMPLE_NEXT_MOMENT)],),
        ([get_range_form("2024-01-15T10:30:00+00:00", "2024-01-16T10:30:00+00:00")],),
        request_values=((SAMPLE_MOMENT, SAMPLE_NEXT_MOMENT),),
    ),
    # ClickHouse's fields.
    FieldSample(
        "clickhouse_date",
        lambda: {"value": clickhouse_fields.ClickhouseDateField(null=True)},
        (datetime.date(2024, 1, 15),),
        ("2024-01-15",),
    ),
    FieldSample(
        "clickhouse_datetime",
        lambda: {"value": clickhouse_fields.ClickhouseDatetimeField(null=True)},
        (SAMPLE_MOMENT,),
        ("2024-01-15T10:30:00+00:00",),
    ),
    FieldSample(
        "clickhouse_integer", lambda: {"value": clickhouse_fields.ClickhouseIntegerField(null=True)}, (9,), (9,)
    ),
    FieldSample(
        "dynamic",
        lambda: {"value": clickhouse_fields.DynamicField(null=True)},
        (5, "abc"),
        (5, "abc"),
        request_filter_refused=True,
    ),
    FieldSample(
        "fixed_string", lambda: {"value": clickhouse_fields.FixedStringField(4, null=True)}, ("abcd",), ("abcd",)
    ),
    FieldSample(
        "float32",
        lambda: {"value": clickhouse_fields.Float32Field(null=True)},
        (1.5, float("nan")),
        (1.5, "NaN"),
        value_dialects=("clickhouse",),
        request_filtered=False,
    ),
    FieldSample("int8", lambda: {"value": clickhouse_fields.Int8Field(null=True)}, (-8,), (-8,)),
    FieldSample("int128", lambda: {"value": clickhouse_fields.Int128Field(null=True)}, (2**100,), (2**100,)),
    FieldSample("int256", lambda: {"value": clickhouse_fields.Int256Field(null=True)}, (-(2**200),), (-(2**200),)),
    FieldSample("uint8", lambda: {"value": clickhouse_fields.UInt8Field(null=True)}, (8,), (8,)),
    FieldSample("uint16", lambda: {"value": clickhouse_fields.UInt16Field(null=True)}, (16,), (16,)),
    FieldSample("uint32", lambda: {"value": clickhouse_fields.UInt32Field(null=True)}, (32,), (32,)),
    FieldSample("uint64", lambda: {"value": clickhouse_fields.UInt64Field(null=True)}, (2**63,), (2**63,)),
    FieldSample("uint128", lambda: {"value": clickhouse_fields.UInt128Field(null=True)}, (2**100,), (2**100,)),
    FieldSample("uint256", lambda: {"value": clickhouse_fields.UInt256Field(null=True)}, (2**200,), (2**200,)),
    FieldSample(
        "low_cardinality", lambda: {"value": clickhouse_fields.LowCardinalityField(fields.TextField())}, ("x",), ("x",)
    ),
    FieldSample(
        "variant",
        lambda: {"value": clickhouse_fields.VariantField([fields.CharField(max_length=5), fields.IntField()])},
        (5, "ab"),
        (5, "ab"),
        request_filter_refused=True,
    ),
    # Geometries - as GeoJSON - and vectors.
    FieldSample(
        "geometry",
        lambda: {"value": GeometryField(null=True)},
        (Point(1, 2),),
        ({"type": "Point", "coordinates": [1.0, 2.0]},),
    ),
    FieldSample(
        "point",
        lambda: {"value": PointField(null=True)},
        (Point(1, 2),),
        ({"type": "Point", "coordinates": [1.0, 2.0]},),
    ),
    FieldSample(
        "line_string",
        lambda: {"value": LineStringField(null=True)},
        (LineString([(0, 0), (1, 1)]),),
        ({"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]},),
    ),
    FieldSample(
        "polygon",
        lambda: {"value": PolygonField(null=True)},
        (Polygon(SQUARE),),
        ({"type": "Polygon", "coordinates": [[list(position) for position in SQUARE]]},),
    ),
    FieldSample(
        "multi_point",
        lambda: {"value": MultiPointField(null=True)},
        (MultiPoint([Point(1, 2)]),),
        ({"type": "MultiPoint", "coordinates": [[1.0, 2.0]]},),
    ),
    FieldSample(
        "multi_line_string",
        lambda: {"value": MultiLineStringField(null=True)},
        (MultiLineString([LineString([(0, 0), (1, 1)])]),),
        ({"type": "MultiLineString", "coordinates": [[[0.0, 0.0], [1.0, 1.0]]]},),
    ),
    FieldSample(
        "multi_polygon",
        lambda: {"value": MultiPolygonField(null=True)},
        (MultiPolygon([Polygon(SQUARE)]),),
        ({"type": "MultiPolygon", "coordinates": [[[list(position) for position in SQUARE]]]},),
    ),
    FieldSample(
        "geometry_collection",
        lambda: {"value": GeometryCollectionField(null=True)},
        (GeometryCollection([Point(1, 2)]),),
        ({"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1.0, 2.0]}]},),
    ),
    FieldSample(
        "vector",
        lambda: {"value": VectorField(3, null=True)},
        ([1.0, 2.0, 3.0],),
        ([1.0, 2.0, 3.0],),
    ),
)
