"""Geometry values and the readers - WKT, EWKT, (E)WKB and GeoJSON in and out, without a database."""

from __future__ import annotations

import math
import struct

import pytest

from hare.dialects.base.types import TypeMapping, TypeRegistry
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from hare.gis import (
    GeometryCollection,
    GeometryField,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    PointField,
    Polygon,
)
from hare.gis.fields.extent_field import ExtentField
from hare.gis.readers import EwkbReader, GeoJsonReader, WktReader
from hare.migrations.writer.import_manager import ImportManager
from hare.migrations.writer.migration_writer import MigrationWriter

SQUARE = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
HOLE = [(1, 1), (2, 1), (2, 2), (1, 1)]

GEOMETRIES = [
    Point(1, 2),
    Point(1.5, -2.25, 3, srid=4326),
    Point(),
    LineString([(0, 0), (1, 1), (2, 0)], srid=3857),
    LineString([]),
    Polygon(SQUARE, holes=[HOLE], srid=4326),
    Polygon(),
    MultiPoint([(0, 0), Point(1, 1)]),
    MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]], srid=4326),
    MultiPolygon([Polygon(SQUARE), [HOLE]]),
    GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])], srid=4326),
    GeometryCollection(),
]


class SquareLike:
    """A shapely-like object - GeoJSON through ``__geo_interface__``."""

    __geo_interface__ = {"type": "Polygon", "coordinates": [SQUARE]}


@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda geometry: geometry.ewkt)
def test_wkt_reads_back_the_same_geometry(geometry):
    assert WktReader.read(geometry.ewkt) == geometry
    assert WktReader.read(geometry.wkt) == geometry.with_srid(None)


@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda geometry: geometry.ewkt)
def test_geo_json_reads_back_the_same_geometry(geometry):
    assert GeoJsonReader.read(geometry.__geo_interface__) == geometry.with_srid(None)


@pytest.mark.database_independent
def test_wkt_forms():
    assert Point(1, 2).wkt == "POINT (1 2)"
    assert Point(1.5, 2, 3, srid=4326).ewkt == "SRID=4326;POINT Z (1.5 2 3)"
    assert Polygon(SQUARE).wkt == "POLYGON ((0 0,4 0,4 4,0 4,0 0))"
    assert MultiPoint([(0, 0), (1, 1)]).wkt == "MULTIPOINT ((0 0),(1 1))"
    assert GeometryCollection([Point(0, 0)]).wkt == "GEOMETRYCOLLECTION (POINT (0 0))"
    assert LineString([]).wkt == "LINESTRING EMPTY"
    assert WktReader.read("srid=3857; multipoint(0 0, 1 1)") == MultiPoint([(0, 0), (1, 1)], srid=3857)
    assert WktReader.read("POINT Z(1 2 3)") == Point(1, 2, 3)
    assert WktReader.read("MULTIPOLYGON(((0 0,4 0,4 4,0 0)),EMPTY)") == MultiPolygon(
        [[[(0, 0), (4, 0), (4, 4), (0, 0)]], Polygon()]
    )


@pytest.mark.database_independent
def test_wkt_white_space_around_and_between_the_tokens_is_read_past():
    """The reader looked for the end of the text by copying what was left of it at every token."""
    assert WktReader.read(" \tPOINT ( 1\n2 ) \r\n ") == Point(1, 2)
    assert WktReader.read("LINESTRING(0 0,1 1)　") == LineString([(0, 0), (1, 1)])
    line = LineString([(index, index) for index in range(20000)])
    assert WktReader.read(line.wkt + "  ") == line


@pytest.mark.database_independent
def test_ewkb_reads_postgis_and_iso_forms():
    little_endian_point = struct.pack("<BIIdd", 1, 0x20000001, 4326, 1.0, 2.0)
    assert EwkbReader.read(little_endian_point) == Point(1, 2, srid=4326)
    assert EwkbReader.read(little_endian_point.hex()) == Point(1, 2, srid=4326)
    big_endian_point_z = struct.pack(">BIddd", 0, 1001, 1.0, 2.0, 3.0)
    assert EwkbReader.read(big_endian_point_z) == Point(1, 2, 3)
    empty_point = struct.pack("<BIdd", 1, 1, math.nan, math.nan)
    assert EwkbReader.read(empty_point) == Point()
    line = struct.pack("<BII", 1, 2, 2) + struct.pack("<dddd", 0, 0, 1, 1)
    collection = struct.pack("<BIII", 1, 0x20000007, 3857, 1) + line
    assert EwkbReader.read(collection) == GeometryCollection([LineString([(0, 0), (1, 1)])], srid=3857)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x02\x01\x00\x00\x00",
        struct.pack("<BI", 1, 99),
        struct.pack("<BIddd", 1, 2001, 1.0, 2.0, 3.0),
        struct.pack("<BIdd", 1, 1, 1.0, 2.0) + b"\x00",
        "zz",
    ],
)
def test_ewkb_refuses_what_isnt_a_geometry(data):
    with pytest.raises(ValidationError, match="Not a WKB geometry"):
        EwkbReader.read(data)


@pytest.mark.parametrize(
    "text",
    [
        "POINT(1)",
        "POINT(1 2",
        "CIRCLE(1 2)",
        "POINT M(1 2 3)",
        "POINT(1 2) x",
        "GEOMETRY(1 2)",
        "POINT(a b)",
        "LINESTRING(0 0)",
    ],
)
def test_wkt_refuses_what_isnt_a_geometry(text):
    with pytest.raises(ValidationError, match="Not a WKT geometry"):
        WktReader.read(text)


@pytest.mark.parametrize(
    "value",
    [{"type": "Circle", "coordinates": []}, {"type": "Point"}, {"type": "GeometryCollection"}, [1, 2], "POINT(1 2)"],
)
def test_geo_json_refuses_what_isnt_a_geometry(value):
    with pytest.raises(ValidationError):
        GeoJsonReader.read(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "build",
    [
        lambda: Point(1, None),
        lambda: Point(1, math.inf),
        lambda: Point(True, 2),
        lambda: LineString([(0, 0)]),
        lambda: LineString([(0, 0), (1, 1, 1)]),
        lambda: Polygon([(0, 0), (1, 0), (0, 0)]),
        lambda: Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        lambda: Polygon(holes=[HOLE]),
        lambda: MultiPoint([LineString([(0, 0), (1, 1)])]),
        lambda: MultiPoint([(0, 0), (1, 1, 1)]),
        lambda: GeometryCollection([(0, 0)]),
        lambda: Point(1, 2, srid=-1),
        lambda: Point(1, 2, srid=1_000_000),
    ],
)
def test_a_wrong_geometry_is_refused(build):
    with pytest.raises(ValidationError):
        build()


@pytest.mark.database_independent
def test_geometries_are_immutable_values():
    point = Point(1, 2, srid=4326)
    with pytest.raises(AttributeError, match="immutable"):
        point.x = 5
    assert point.with_srid(3857) == Point(1, 2, srid=3857)
    assert point.srid == 4326
    assert hash(point) == hash(Point(1, 2, srid=4326))
    assert point != Point(1, 2)
    assert Point(1, 2).with_srid(0).srid is None
    assert repr(point) == "Point('SRID=4326;POINT (1 2)')"
    assert Polygon(SQUARE, holes=[HOLE]).holes == (tuple((float(x), float(y)) for x, y in HOLE),)


@pytest.mark.database_independent
def test_the_field_takes_every_form():
    field = PointField()
    field.model_field_name = "location"
    expected = Point(1, 2, srid=4326)
    point_ewkb = struct.pack("<BIIdd", 1, 0x20000001, 4326, 1.0, 2.0)
    for value in (
        Point(1, 2),
        "POINT(1 2)",
        "SRID=4326;POINT(1 2)",
        point_ewkb,
        point_ewkb.hex(),
        {"type": "Point", "coordinates": [1, 2]},
    ):
        assert field.to_python(value) == expected
        assert field.to_db_value(value, None) == "SRID=4326;POINT (1 2)"
    assert GeometryField().to_python(SquareLike()) == Polygon(SQUARE, srid=4326)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (PointField(), Polygon(SQUARE), "expected a POINT, got a POLYGON"),
        (PointField(), Point(1, 2, 3), "expected 2 coordinates, got 3"),
        (PointField(dimensions=3), Point(1, 2), "expected 3 coordinates, got 2"),
        (PointField(), Point(1, 2, srid=3857), "SRID 3857 isn't the column's 4326"),
        (PointField(), 5, "expected a geometry"),
        (PointField(), "POINT(1)", "Not a WKT geometry"),
    ],
)
def test_the_field_refuses_a_wrong_value(field, value, message):
    field.model_field_name = "location"
    with pytest.raises(ValidationError, match=message):
        field.to_db_value(value, None)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "kwargs",
    [
        {"geometry_type": "CIRCLE"},
        {"srid": 0},
        {"srid": 1_000_000},
        {"srid": "4326"},
        {"dimensions": 4},
        {"dimensions": True},
    ],
)
def test_a_wrong_field_argument_is_refused(kwargs):
    with pytest.raises(ConfigurationError):
        GeometryField(**kwargs)


@pytest.mark.database_independent
def test_extent_field_reads_a_box_or_a_polygon():
    field = ExtentField()
    assert field.to_python("BOX(1 2,3.5 4)") == (1.0, 2.0, 3.5, 4.0)
    assert field.to_python(Polygon(SQUARE)) == (0.0, 0.0, 4.0, 4.0)
    assert field.to_python(None) is None
    with pytest.raises(ValidationError):
        field.to_python("BOX(1 2)")


class SpatialLessDialect(SqliteDialect):
    """SQLite's dialect without a spatial column type."""

    name = "spatial_less"

    def build_types(self) -> TypeRegistry:
        types = super().build_types()
        types.register(GeometryField, TypeMapping())
        return types


@pytest.mark.database_independent
def test_a_database_without_spatial_types_refuses_the_field():
    # SQLite stores SpatiaLite's own BLOB; a dialect registering no spatial column type refuses the field.
    assert PointField().get_column_type(DialectRegistry.get_dialect("sqlite")) == "POINT"
    with pytest.raises(UnSupportedError):
        PointField().get_column_type(SpatialLessDialect())


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "geometry",
    [
        Point(1.5, -2, srid=4326),
        Point(1, 2, 3),
        Point(),
        LineString([(0, 0), (1, 1)], srid=3857),
        Polygon([(0, 0), (4, 0), (4, 4), (0, 0)], [[(1, 1), (2, 1), (2, 2), (1, 1)]], srid=4326),
        Polygon([(0, 0), (4, 0), (4, 4), (0, 0)]),
        MultiPoint([Point(0, 0), Point(1, 1)], srid=4326),
        MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])]),
        GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])], srid=4326),
        MultiLineString(),
    ],
)
def test_a_geometry_is_written_into_a_migration_as_the_call_building_it(geometry):
    # A field's default: the migration imports the class and rebuilds the same geometry.
    imports = ImportManager()
    code = MigrationWriter.render_value(geometry, imports)
    namespace: dict = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102 - the code the writer generated
    assert eval(code, namespace) == geometry  # noqa: S307


@pytest.mark.database_independent
def test_collections_nested_too_deep_are_refused():
    """A deep enough nesting ran past Python's recursion limit - a raw RecursionError on reading it."""
    depth = 3000
    wkt = "GEOMETRYCOLLECTION(" * depth + "POINT(1 2)" + ")" * depth
    with pytest.raises(ValidationError, match="nested more than 100 deep"):
        WktReader.read(wkt)
    member = struct.pack("<BIdd", 1, 1, 1.0, 2.0)
    for _level in range(depth):
        member = struct.pack("<BII", 1, 7, 1) + member
    with pytest.raises(ValidationError, match="nested more than 100 deep"):
        EwkbReader.read(member)
    geo_json: dict = {"type": "Point", "coordinates": [1, 2]}
    for _level in range(depth):
        geo_json = {"type": "GeometryCollection", "geometries": [geo_json]}
    with pytest.raises(ValidationError, match="nested more than 100 deep"):
        GeoJsonReader.read(geo_json)
    holding_itself: dict = {"type": "GeometryCollection", "geometries": []}
    holding_itself["geometries"].append(holding_itself)
    with pytest.raises(ValidationError, match="nested more than 100 deep"):
        GeoJsonReader.read(holding_itself)
    collection = GeometryCollection([Point(1, 2)])
    with pytest.raises(ValidationError, match="nested more than 100 deep"):
        for _level in range(depth):
            collection = GeometryCollection([collection])
    shallow = "GEOMETRYCOLLECTION(" * 50 + "POINT(1 2)" + ")" * 50
    assert WktReader.read(shallow).get_nesting_depth() == 50


@pytest.mark.database_independent
def test_an_equality_with_a_geometry_of_another_srid_is_refused_on_sqlite():
    """SpatiaLite compared the BLOB of another SRID with the column's - no row equal, every row not."""
    from hare.dialects.sqlite.spatial.sqlite_geometry_values import SqliteGeometryValues

    with pytest.raises(ValidationError, match="SRID 3857 isn't the column's 4326"):
        SqliteGeometryValues.to_lookup(PointField(), Point(1, 2, srid=3857), None)
    assert SqliteGeometryValues.to_lookup(PointField(), Point(1, 2), None) is not None
    # Another type or other dimensions likewise - a write refused them, PostgreSQL did in a filter too.
    for field, value, message in (
        (PointField(), LineString([(0, 0), (1, 1)]), "expected a POINT, got a LINESTRING"),
        (PointField(), Point(1, 2, 3), "expected 2 coordinates, got 3"),
        (PointField(dimensions=3), Point(1, 2), "expected 3 coordinates, got 2"),
    ):
        with pytest.raises(ValidationError, match=message):
            SqliteGeometryValues.to_lookup(field, value, None)


@pytest.mark.database_independent
@pytest.mark.parametrize("distance", [10**400, -1, float("nan"), float("inf"), True, "1"], ids=repr)
def test_a_distance_that_is_no_finite_number_of_zero_or_more_is_refused(distance):
    """An int past the float range raised OverflowError out of float() - not the promised ValidationError."""
    from hare.gis.lookups.spatial_field_lookups import SpatialFieldLookups

    field = PointField(srid=4326)
    field.model_field_name = "spot"
    with pytest.raises(ValidationError, match="a distance is a finite number of zero or more"):
        SpatialFieldLookups.encode_geometry_with_distance(
            (Point(1, 1, srid=4326), distance), None, field, DialectRegistry.get_dialect("sqlite")
        )
