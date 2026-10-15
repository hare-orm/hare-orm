"""hare.gis on SQLite: geometries stored as SpatiaLite's own BLOB - written and read by hare without the
extension - and the spatial lookups, paths, functions and aggregates through SpatiaLite
(``load_spatialite=True``), the same API as on PostGIS. A geography is a longitude/latitude geometry
measured on the ellipsoid. The tests needing SpatiaLite skip where HARE_TEST_SPATIALITE_PATH names no
library."""

from __future__ import annotations

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.dialects.sqlite.spatial.spatialite_blob_reader import SpatialiteBlobReader
from hare.dialects.sqlite.spatial.spatialite_blob_writer import SpatialiteBlobWriter
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from hare.gis import GeometryCollection, LineString, MultiPoint, MultiPolygon, Point, Polygon
from hare.gis.aggregates import Collect, Extent, MakeLine, UnionAggregate
from hare.gis.functions import (
    Area,
    AsGeoJSON,
    AsText,
    Boundary,
    Buffer,
    Centroid,
    ConvexHull,
    Difference,
    Distance,
    Envelope,
    GeometryTypeName,
    Intersection,
    IsValid,
    Length,
    MakeValid,
    NumGeometries,
    NumPoints,
    Perimeter,
    PointOnSurface,
    Simplify,
    SymmetricDifference,
    Transform,
    Union,
)
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.query.expressions import F
from tests.dialects.sqlite.models_spatialite import SpatialCity, SpatialPlace, SpatialSite
from tests.spatialite.spatialite_settings import SpatialiteTestSettings

MODULES = ["tests.dialects.sqlite.models_spatialite"]
SQUARE = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
SMALL_SQUARE = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]
MOSCOW = Point(37.6173, 55.7558)
SAINT_PETERSBURG = Point(30.3351, 59.9343)
#: Geometries of every type and dimension the codec writes - as SpatiaLite's GeomFromEWKT() writes them.
CODEC_GEOMETRIES = [
    Point(1, 2, srid=4326),
    Point(1.5, -2.25, 3, srid=4326),
    LineString([(0, 0), (1, 1), (2, 0)], srid=4326),
    LineString([(0, 0, 1), (1, 1, 2)], srid=3857),
    Polygon(SQUARE, holes=[[(1, 1), (2, 1), (2, 2), (1, 1)]], srid=4326),
    MultiPoint([(0, 0), (1, 1)], srid=4326),
    MultiPolygon([[[(0, 0), (1, 0), (1, 1), (0, 0)]]], srid=4326),
    GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])], srid=4326),
]


@pytest_asyncio.fixture
async def spatial_db():
    async with hare_test_context(
        MODULES, db_url=SpatialiteTestSettings.get_db_url(), app_label="models", connection_label="models"
    ) as context:
        yield context


@pytest_asyncio.fixture
async def plain_db():
    async with hare_test_context(
        MODULES, db_url="sqlite+aiosqlite://:memory:", app_label="models", connection_label="models"
    ) as context:
        yield context


async def create_places() -> None:
    await SpatialPlace.objects.create(id=1, name="inside", location=Point(1, 1), area=Polygon(SQUARE))
    await SpatialPlace.objects.create(id=2, name="edge", location="POINT(4 2)", area=Polygon(SMALL_SQUARE))
    await SpatialPlace.objects.create(
        id=3,
        name="far",
        location={"type": "Point", "coordinates": [10, 10]},
        route=LineString([(0, 0), (3, 4)]),
        shape=MultiPoint([(5, 5), (6, 6)]),
    )


async def get_ids(**kwargs) -> list[int]:
    return list(await SpatialPlace.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))


def test_the_column_types():
    dialect = DialectRegistry.get_dialect("sqlite")
    columns = {name: field.get_column_type(dialect) for name, field in SpatialPlace._meta.fields_map.items()}
    assert (columns["location"], columns["area"], columns["shape"], columns["height_point"]) == (
        "POINT",
        "POLYGON",
        "GEOMETRY",
        "POINTZ",
    )


@pytest.mark.parametrize("geometry", CODEC_GEOMETRIES, ids=lambda geometry: geometry.wkt)
def test_the_codec_reads_what_it_writes(geometry):
    assert SpatialiteBlobReader.read(SpatialiteBlobWriter.write(geometry)) == geometry


def test_the_codec_refuses_what_spatialite_has_no_form_of():
    with pytest.raises(ValidationError, match="no empty geometry"):
        SpatialiteBlobWriter.write(Point(srid=4326))
    with pytest.raises(ValidationError, match="no empty geometry"):
        SpatialiteBlobWriter.write(GeometryCollection([Point(0, 0), LineString([])], srid=4326))
    with pytest.raises(ValidationError, match="Not a SpatiaLite geometry"):
        SpatialiteBlobReader.read(b"\x00\x01")
    measured = bytes.fromhex(
        "0001E6100000000000000000F03F0000000000000040000000000000F03F00000000000000407C"
        "D1070000000000000000F03F00000000000000400000000000001440FE"
    )
    with pytest.raises(ValidationError, match="measured"):
        SpatialiteBlobReader.read(measured)
    with pytest.raises(ValidationError, match="unknown byte order 7"):
        SpatialiteBlobReader.read(measured[:1] + b"\x07" + measured[2:])


@pytest.mark.asyncio
async def test_geometries_are_stored_without_the_extension(plain_db):
    await create_places()
    place = await SpatialPlace.objects.get(id=1)
    assert place.location == Point(1, 1, srid=4326)
    assert place.area == Polygon(SQUARE, srid=4326)
    assert await get_ids(location="POINT(4 2)") == [2]
    assert await get_ids(shape__isnull=False) == [3]
    with pytest.raises(ValidationError, match="no empty geometry"):
        await SpatialPlace.objects.create(id=9, name="empty", location=Point())
    with pytest.raises(UnSupportedError, match="supports_spatial"):
        await get_ids(location__intersects=Polygon(SQUARE))
    with pytest.raises(UnSupportedError, match="supports_spatial"):
        await get_ids(location__x__gte=1)
    with pytest.raises(UnSupportedError, match="supports_spatial"):
        await SpatialPlace.objects.annotate(size=Area("area")).values_list("size", flat=True)
    with pytest.raises(UnSupportedError, match="supports_spatial"):
        await SpatialPlace.objects.aggregate(points=Collect("location"))


def test_the_spatialite_settings_are_checked():
    from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient

    with pytest.raises(ConfigurationError, match="only load_spatialite=True"):
        AiosqliteClient(connection_alias="spatial", file_path=":memory:", spatialite_path="mod_spatialite")
    with pytest.raises(ConfigurationError, match="non-empty"):
        AiosqliteClient(connection_alias="spatial", file_path=":memory:", load_spatialite=True, spatialite_path="")


@pytest.mark.asyncio
async def test_a_library_that_doesnt_load_is_refused():
    with pytest.raises(ConfigurationError, match="SpatiaLite can't be loaded"):
        async with hare_test_context(
            MODULES,
            db_url="sqlite+aiosqlite://:memory:?load_spatialite=true&spatialite_path=no_such_spatialite_library",
        ):
            pass


@pytest.mark.asyncio
async def test_the_codec_matches_spatialite(spatial_db):
    connection = Connections.get("models")
    for geometry in CODEC_GEOMETRIES:
        ewkt = f"SRID={geometry.srid};{geometry.wkt.replace(' Z ', ' ').replace(' Z(', '(')}"
        _, rows = await connection.execute("SELECT GeomFromEWKT(?) AS geometry", [ewkt])
        assert rows[0]["geometry"] == SpatialiteBlobWriter.write(geometry), geometry.wkt
    compressible = [
        LineString([(0, 0), (1, 1), (2, 0), (3, 3)], srid=4326),
        Polygon(SQUARE, srid=4326),
        LineString([(0, 0, 1), (1, 1, 2), (2, 0, 3), (3, 3, 4)], srid=4326),
    ]
    for geometry in compressible:
        _, rows = await connection.execute(
            "SELECT CompressGeometry(?) AS geometry", [SpatialiteBlobWriter.write(geometry)]
        )
        assert SpatialiteBlobReader.read(rows[0]["geometry"]) == geometry


@pytest.mark.asyncio
async def test_spatial_relations(spatial_db):
    await create_places()
    big_square = Polygon([(-1, -1), (5, -1), (5, 5), (-1, 5), (-1, -1)])
    assert await get_ids(location__within=big_square) == [1, 2]
    assert await get_ids(location__intersects=Polygon(SQUARE)) == [1, 2]
    assert await get_ids(location__covered_by=Polygon(SQUARE)) == [1, 2]
    assert await get_ids(location__touches=Polygon(SQUARE)) == [2]
    assert await get_ids(location__disjoint=Polygon(SQUARE)) == [3]
    assert await get_ids(area__contains=Point(3, 3)) == [1]
    assert await get_ids(area__contains_properly=Point(4, 4)) == []
    assert await get_ids(area__contains_properly=Point(3, 3)) == [1]
    assert await get_ids(area__covers=Point(4, 4)) == [1]
    assert await get_ids(area__overlaps=Polygon([(3, 3), (6, 3), (6, 6), (3, 6), (3, 3)])) == [1]
    assert await get_ids(route__crosses=LineString([(0, 4), (4, 0)])) == [3]
    assert await get_ids(location__equals="POINT(1 1)") == [1]
    assert await get_ids(location__bbox_overlaps=big_square) == [1, 2]
    assert await get_ids(area__bbox_contains=Point(1.5, 1.5)) == [1, 2]
    assert await get_ids(area__bbox_contained=big_square) == [1, 2]
    assert await get_ids(area__relate=(Point(2, 2), "T********")) == [1]
    assert await get_ids(location__within=F("area")) == [1]
    # A NULL column is unknown - SpatiaLite's -1 is no match.
    assert await get_ids(area__intersects=Point(10, 10)) == []
    assert await get_ids(route__disjoint=Point(100, 100)) == [3]


@pytest.mark.asyncio
async def test_distance_lookups(spatial_db):
    await create_places()
    assert await get_ids(location__dwithin=(Point(0, 0), 1.5)) == [1]
    assert await get_ids(location__distance_lte=(Point(0, 0), 4.5)) == [1, 2]
    assert await get_ids(location__distance_lt=(Point(0, 0), 20**0.5)) == [1]
    assert await get_ids(location__distance_gt=(Point(0, 0), 5)) == [3]
    assert await get_ids(location__distance_gte=(Point(0, 0), 20**0.5)) == [2, 3]


@pytest.mark.asyncio
async def test_validity(spatial_db):
    await create_places()
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    await SpatialPlace.objects.create(id=4, name="bowtie", location=Point(0, 0), area=bowtie)
    assert await get_ids(area__isvalid=False) == [4]
    assert await get_ids(area__isvalid=True) == [1, 2]
    repaired = (
        await SpatialPlace.objects.filter(id=4).annotate(fixed=MakeValid("area")).values_list("fixed", flat=True)
    )
    assert repaired[0].GEOMETRY_TYPE == "MULTIPOLYGON"


@pytest.mark.asyncio
async def test_a_value_in_another_srid_is_transformed(spatial_db):
    await create_places()
    web_mercator_point = Point(111319.49079327357, 111325.14286638486, srid=3857)
    assert await get_ids(location__dwithin=(web_mercator_point, 1e-6)) == [1]
    await SpatialSite.objects.create(id=1, position=Point(100, 200, srid=3857))
    lon_lat = Point(0.0008983152841195215, 0.0017966305681926, srid=4326)
    assert await SpatialSite.objects.filter(position__dwithin=(lon_lat, 1)).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_paths(spatial_db):
    await create_places()
    await SpatialPlace.objects.create(id=4, name="high", location=Point(0, 0), height_point=Point(1, 2, 3))
    assert await SpatialPlace.objects.filter(id__in=[1, 2]).order_by("id").values_list(
        "location__x", "location__y", "location__srid"
    ) == [(1.0, 1.0, 4326), (4.0, 2.0, 4326)]
    assert await get_ids(location__x__gte=4) == [2, 3]
    assert await SpatialPlace.objects.filter(id=4).values_list("height_point__z", flat=True) == [3.0]
    assert (await SpatialPlace.objects.get(id=4)).height_point == Point(1, 2, 3, srid=4326)


@pytest.mark.asyncio
async def test_measure_and_geometry_functions(spatial_db):
    await create_places()
    first = (
        await SpatialPlace.objects.filter(id=1)
        .annotate(
            area_size=Area("area"),
            perimeter=Perimeter("area"),
            distance=Distance("location", Point(4, 5)),
            centroid=Centroid("area"),
            envelope=Envelope("area"),
            on_surface=PointOnSurface("area"),
            boundary=Boundary("area"),
            hull=ConvexHull("area"),
            text=AsText("location"),
            geo_json=AsGeoJSON("location"),
            points=NumPoints(Boundary("area")),
            members=NumGeometries("area"),
            type_name=GeometryTypeName("area"),
            valid=IsValid("area"),
            projected=Transform("location", 3857),
        )
        .values()
    )[0]
    assert first["area_size"] == 16.0
    assert first["perimeter"] == 16.0
    assert first["distance"] == 5.0
    assert first["centroid"] == Point(2, 2, srid=4326)
    assert first["envelope"].GEOMETRY_TYPE == "POLYGON"
    assert first["on_surface"].GEOMETRY_TYPE == "POINT"
    assert first["boundary"] == LineString(SQUARE, srid=4326)
    assert first["hull"].GEOMETRY_TYPE == "POLYGON"
    assert first["text"] == "POINT(1 1)"
    assert first["geo_json"] == {"type": "Point", "coordinates": [1, 1]}
    assert first["points"] == 5
    assert first["members"] == 1
    assert first["type_name"] == "POLYGON"
    assert first["valid"] is True
    assert first["projected"].srid == 3857
    assert round(first["projected"].x, 3) == 111319.491
    third = (
        await SpatialPlace.objects.filter(id=3)
        .annotate(route_length=Length("route"), simple=Simplify("route", 0.5))
        .values("route_length", "simple")
    )[0]
    assert third == {"route_length": 5.0, "simple": LineString([(0, 0), (3, 4)], srid=4326)}
    await SpatialPlace.objects.create(id=4, name="high", location=Point(0, 0), height_point=Point(1, 2, 3))
    assert await SpatialPlace.objects.filter(id=4).annotate(t=GeometryTypeName("height_point")).values_list(
        "t", flat=True
    ) == ["POINT"]


@pytest.mark.asyncio
async def test_overlay_functions(spatial_db):
    await create_places()
    other = Polygon([(2, 2), (6, 2), (6, 6), (2, 6), (2, 2)])
    row = (
        await SpatialPlace.objects.filter(id=1)
        .annotate(
            common=Area(Intersection("area", other)),
            only_first=Area(Difference("area", other)),
            either=Area(SymmetricDifference("area", other)),
            merged=Area(Union("area", other)),
            buffered=Area(Buffer("location", 1)),
            to_column=Area(Intersection("area", F("area"))),
        )
        .values()
    )[0]
    assert (row["common"], row["only_first"], row["either"], row["merged"], row["to_column"]) == (
        4.0,
        12.0,
        24.0,
        28.0,
        16.0,
    )
    assert 3.1 < row["buffered"] < 3.15


@pytest.mark.asyncio
async def test_aggregates(spatial_db):
    await create_places()
    result = await SpatialPlace.objects.filter(id__in=[1, 2]).aggregate(
        points=Collect("location"),
        extent=Extent("area"),
        merged=UnionAggregate("area"),
    )
    assert set(result["points"].members) == {Point(1, 1), Point(4, 2)}
    assert result["points"].srid == 4326
    assert result["extent"] == (0.0, 0.0, 4.0, 4.0)
    assert result["merged"].GEOMETRY_TYPE == "POLYGON"
    distinct = await SpatialPlace.objects.aggregate(points=Collect("location", distinct=True))
    assert len(distinct["points"].members) == 3


@pytest.mark.asyncio
async def test_an_ordered_line(spatial_db):
    # Connected first - the features of the server's version are known then.
    await create_places()
    if not Connections.get("models").features.supports_ordered_aggregates:
        pytest.skip("an aggregate's ORDER BY needs features.supports_ordered_aggregates")
    result = await SpatialPlace.objects.filter(id__in=[1, 2]).aggregate(line=MakeLine("location", order_by="-id"))
    assert result["line"] == LineString([(4, 2), (1, 1)], srid=4326)


@pytest.mark.asyncio
async def test_an_ordered_aggregate_needs_sqlite_3_44(spatial_db):
    await create_places()
    connection = Connections.get("models")
    connection.features = connection.features.replace(supports_ordered_aggregates=False)
    with pytest.raises(UnSupportedError, match="supports_ordered_aggregates"):
        await SpatialPlace.objects.aggregate(line=MakeLine("location", order_by="id"))


@pytest.mark.asyncio
async def test_geography_measures_on_the_ellipsoid(spatial_db):
    await SpatialCity.objects.create(id=1, name="Moscow", center=MOSCOW)
    await SpatialCity.objects.create(id=2, name="Saint Petersburg", center=SAINT_PETERSBURG)
    distance = (
        await SpatialCity.objects.filter(id=1)
        .annotate(d=Distance("center", SAINT_PETERSBURG))
        .values_list("d", flat=True)
    )
    assert 630_000 < distance[0] < 640_000
    near_moscow = await SpatialCity.objects.filter(center__dwithin=(Point(37.6, 55.7), 10_000)).values_list(
        "name", flat=True
    )
    assert near_moscow == ["Moscow"]
    assert await SpatialCity.objects.filter(center__distance_gt=(MOSCOW, 100_000)).values_list("id", flat=True) == [2]
    assert await SpatialCity.objects.filter(id=1).values_list("center__x", "center__srid") == [(37.6173, 4326)]
    region = Polygon([(37, 55), (38, 55), (38, 56), (37, 56), (37, 55)])
    await SpatialCity.objects.filter(id=1).update(boundary=region)
    area = await SpatialCity.objects.filter(id=1).annotate(a=Area("boundary")).values_list("a", flat=True)
    assert 7_000_000_000 < area[0] < 7_100_000_000


@pytest.mark.asyncio
async def test_a_geography_needs_the_spatial_metadata():
    async with hare_test_context(
        MODULES,
        db_url=SpatialiteTestSettings.get_db_url(metadata=SpatialiteMetadata.NONE),
        app_label="models",
        connection_label="models",
    ):
        await SpatialCity.objects.create(id=1, name="Moscow", center=MOSCOW)
        assert (await SpatialCity.objects.get(id=1)).center == MOSCOW.with_srid(4326)
        with pytest.raises(UnSupportedError, match="supports_geography"):
            await SpatialCity.objects.filter(center__dwithin=(MOSCOW, 1000)).count()
        with pytest.raises(UnSupportedError, match="supports_geography"):
            await SpatialCity.objects.annotate(d=Distance("center", SAINT_PETERSBURG)).values_list("d", flat=True)
        # A geometry needs no metadata.
        await SpatialPlace.objects.create(id=1, name="inside", location=Point(1, 1))
        assert await get_ids(location__dwithin=(Point(0, 0), 1.5)) == [1]


@pytest.mark.asyncio
async def test_the_spatial_metadata_is_no_models_table(spatial_db):

    connection = Connections.get("models")
    _, rows = await connection.execute("SELECT CheckSpatialMetaData() AS layout")
    assert rows[0]["layout"] > 0
    assert set(await DatabaseCatalog.get_table_names(connection)) == {
        "spatial_place",
        "spatial_city",
        "spatial_site",
    }


@pytest.mark.asyncio
async def test_a_geography_refuses_what_spatialite_measures_on_the_plane(spatial_db):
    await SpatialCity.objects.create(id=1, name="Moscow", center=MOSCOW)
    region = Polygon([(37, 55), (38, 55), (38, 56), (37, 56), (37, 55)])
    with pytest.raises(UnSupportedError, match="spheroid"):
        await SpatialCity.objects.filter(center__intersects=region).count()
    with pytest.raises(UnSupportedError, match="spheroid"):
        await SpatialCity.objects.annotate(c=Centroid("center")).values_list("c", flat=True)
    with pytest.raises(UnSupportedError, match="spheroid"):
        await SpatialCity.objects.annotate(b=Buffer("center", 1000)).values_list("b", flat=True)
    with pytest.raises(UnSupportedError, match="not a geography"):
        await SpatialCity.objects.aggregate(points=Collect("center"))
