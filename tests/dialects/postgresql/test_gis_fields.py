"""GeometryField on PostGIS: geometry and geography columns written and read in every form, the spatial
lookups, paths, functions and aggregates - and what a geography or another database refuses."""

from __future__ import annotations

import pytest

from hare.dialects.enums import DialectName
from hare.exceptions import UnSupportedError, ValidationError
from hare.gis import GeometryCollection, LineString, MultiPoint, Point, Polygon
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
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.query.expressions import F
from tests.dialects.postgresql.models_gis import GisCity, GisPlace, GisProjectedSite

SQUARE = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
SMALL_SQUARE = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]
MOSCOW = Point(37.6173, 55.7558)
SAINT_PETERSBURG = Point(30.3351, 59.9343)


async def create_places() -> None:
    await GisPlace.objects.create(id=1, name="inside", location=Point(1, 1), area=Polygon(SQUARE))
    await GisPlace.objects.create(id=2, name="edge", location="POINT(4 2)", area=Polygon(SMALL_SQUARE))
    await GisPlace.objects.create(
        id=3,
        name="far",
        location={"type": "Point", "coordinates": [10, 10]},
        route=LineString([(0, 0), (3, 4)]),
        shape=MultiPoint([(5, 5), (6, 6)]),
    )


async def get_ids(**kwargs) -> list[int]:
    return list(await GisPlace.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))


@pytest.mark.asyncio
async def test_values_are_read_as_geometries(db_gis):
    await create_places()
    await GisPlace.objects.create(id=4, name="high", location=Point(0, 0), height_point=Point(1, 2, 3))
    places = {place.id: place for place in await GisPlace.objects.all()}
    assert places[1].location == Point(1, 1, srid=4326)
    assert places[1].area == Polygon(SQUARE, srid=4326)
    assert places[2].location == Point(4, 2, srid=4326)
    assert places[3].route == LineString([(0, 0), (3, 4)], srid=4326)
    assert places[3].shape == MultiPoint([(5, 5), (6, 6)], srid=4326)
    assert places[4].height_point == Point(1, 2, 3, srid=4326)
    assert await GisPlace.objects.filter(id=1).values_list("location", flat=True) == [Point(1, 1, srid=4326)]
    place = GisPlace(id=5, name="memory", location="POINT(7 8)")
    assert place.location == Point(7, 8, srid=4326)


@pytest.mark.asyncio
async def test_spatial_relations(db_gis):
    await create_places()
    big_square = Polygon([(-1, -1), (5, -1), (5, 5), (-1, 5), (-1, -1)])
    assert await get_ids(location__within=big_square) == [1, 2]
    assert await get_ids(location__intersects=Polygon(SQUARE)) == [1, 2]
    assert await get_ids(location__covered_by=Polygon(SQUARE)) == [1, 2]
    assert await get_ids(location__touches=Polygon(SQUARE)) == [2]
    assert await get_ids(location__disjoint=Polygon(SQUARE)) == [3]
    assert await get_ids(area__contains=Point(3, 3)) == [1]
    assert await get_ids(area__contains_properly=Point(4, 4)) == []
    assert await get_ids(area__covers=Point(4, 4)) == [1]
    assert await get_ids(area__overlaps=Polygon([(3, 3), (6, 3), (6, 6), (3, 6), (3, 3)])) == [1]
    assert await get_ids(route__crosses=LineString([(0, 4), (4, 0)])) == [3]
    assert await get_ids(location__equals="POINT(1 1)") == [1]
    assert await get_ids(location__bbox_overlaps=big_square) == [1, 2]
    assert await get_ids(area__bbox_contains=Point(1.5, 1.5)) == [1, 2]
    assert await get_ids(area__bbox_contained=big_square) == [1, 2]
    assert await get_ids(area__relate=(Point(2, 2), "T********")) == [1]
    assert await get_ids(location__within=F("area")) == [1]
    assert await get_ids(location="POINT(1 1)") == [1]
    assert await get_ids(shape__isnull=False) == [3]


@pytest.mark.asyncio
async def test_distance_lookups(db_gis):
    await create_places()
    assert await get_ids(location__dwithin=(Point(0, 0), 1.5)) == [1]
    assert await get_ids(location__distance_lte=(Point(0, 0), 4.5)) == [1, 2]
    assert await get_ids(location__distance_lt=(Point(0, 0), math_sqrt_20())) == [1]
    assert await get_ids(location__distance_gt=(Point(0, 0), 5)) == [3]
    assert await get_ids(location__distance_gte=(Point(0, 0), math_sqrt_20())) == [2, 3]


def math_sqrt_20() -> float:
    return 20**0.5


@pytest.mark.asyncio
async def test_validity_lookup(db_gis):
    await create_places()
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    await GisPlace.objects.create(id=4, name="bowtie", location=Point(0, 0), area=bowtie)
    assert await get_ids(area__isvalid=False) == [4]
    assert await get_ids(area__isvalid=True) == [1, 2]
    repaired = await GisPlace.objects.filter(id=4).annotate(fixed=MakeValid("area")).values_list("fixed", flat=True)
    assert repaired[0].GEOMETRY_TYPE == "MULTIPOLYGON"


@pytest.mark.asyncio
async def test_the_same_query_with_another_geometry_is_not_stale(db_gis):
    await create_places()
    for point, expected_ids in ((Point(1, 1), [1]), (Point(4, 2), [2]), (Point(10, 10), [3])):
        assert await get_ids(location__dwithin=(point, 0.1)) == expected_ids
        assert await get_ids(location__equals=point) == expected_ids
        distances = (
            await GisPlace.objects.order_by("id").annotate(d=Distance("location", point)).values_list("d", flat=True)
        )
        assert distances[expected_ids[0] - 1] == 0


@pytest.mark.asyncio
async def test_a_value_in_another_srid_is_transformed_in_a_filter(db_gis):
    await create_places()
    # (1, 1) in Web Mercator meters.
    web_mercator_point = Point(111319.49079327357, 111325.14286638486, srid=3857)
    assert await get_ids(location__dwithin=(web_mercator_point, 1e-6)) == [1]
    with pytest.raises(ValidationError, match="SRID 3857 isn't the column's 4326"):
        await GisPlace.objects.create(id=9, name="wrong", location=web_mercator_point)


@pytest.mark.asyncio
async def test_paths(db_gis):
    await create_places()
    await GisPlace.objects.create(id=4, name="high", location=Point(0, 0), height_point=Point(1, 2, 3))
    assert await GisPlace.objects.filter(id__in=[1, 2]).order_by("id").values_list(
        "location__x", "location__y", "location__srid"
    ) == [(1.0, 1.0, 4326), (4.0, 2.0, 4326)]
    assert await get_ids(location__x__gte=4) == [2, 3]
    assert await GisPlace.objects.filter(id=4).values_list("height_point__z", flat=True) == [3.0]


@pytest.mark.asyncio
async def test_measure_and_geometry_functions(db_gis):
    await create_places()
    first = (
        await GisPlace.objects.filter(id=1)
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
    assert first["envelope"] == Polygon([(0, 0), (0, 4), (4, 4), (4, 0), (0, 0)], srid=4326)
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
        await GisPlace.objects.filter(id=3)
        .annotate(route_length=Length("route"), simple=Simplify("route", 0.5))
        .values("route_length", "simple")
    )[0]
    assert third == {"route_length": 5.0, "simple": LineString([(0, 0), (3, 4)], srid=4326)}


@pytest.mark.asyncio
async def test_overlay_functions(db_gis):
    await create_places()
    other = Polygon([(2, 2), (6, 2), (6, 6), (2, 6), (2, 2)])
    row = (
        await GisPlace.objects.filter(id=1)
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
async def test_aggregates(db_gis):
    await create_places()
    result = await GisPlace.objects.filter(id__in=[1, 2]).aggregate(
        points=Collect("location"),
        extent=Extent("area"),
        merged=UnionAggregate("area"),
        line=MakeLine("location", order_by="-id"),
    )
    assert set(result["points"].members) == {Point(1, 1), Point(4, 2)}
    assert result["points"].srid == 4326
    assert result["extent"] == (0.0, 0.0, 4.0, 4.0)
    assert result["merged"] == Polygon([(0, 4), (4, 4), (4, 0), (0, 0), (0, 4)], srid=4326)
    assert result["line"] == LineString([(4, 2), (1, 1)], srid=4326)
    distinct = await GisPlace.objects.aggregate(points=Collect("location", distinct=True))
    assert len(distinct["points"].members) == 3
    collection = await GisPlace.objects.aggregate(all_shapes=Collect("shape"))
    assert collection["all_shapes"] == GeometryCollection([MultiPoint([(5, 5), (6, 6)])], srid=4326)


@pytest.mark.asyncio
async def test_geography_measures_in_meters(db_gis):
    await GisCity.objects.create(id=1, name="Moscow", center=MOSCOW)
    await GisCity.objects.create(id=2, name="Saint Petersburg", center=SAINT_PETERSBURG)
    distance = (
        await GisCity.objects.filter(id=1).annotate(d=Distance("center", SAINT_PETERSBURG)).values_list("d", flat=True)
    )
    assert 630_000 < distance[0] < 640_000
    near_moscow = await GisCity.objects.filter(center__dwithin=(Point(37.6, 55.7), 10_000)).values_list(
        "name", flat=True
    )
    assert near_moscow == ["Moscow"]
    assert await GisCity.objects.filter(center__distance_gt=(MOSCOW, 100_000)).values_list("id", flat=True) == [2]
    assert await GisCity.objects.filter(id=1).values_list("center__x", "center__srid") == [(37.6173, 4326)]
    assert (await GisCity.objects.get(id=1)).center == MOSCOW.with_srid(4326)
    region = Polygon([(37, 55), (38, 55), (38, 56), (37, 56), (37, 55)])
    assert await GisCity.objects.filter(center__intersects=region).values_list("id", flat=True) == [1]
    area = await GisCity.objects.filter(id=1).annotate(a=Area(Buffer("center", 1000))).values_list("a", flat=True)
    assert 3_100_000 < area[0] < 3_150_000


@pytest.mark.asyncio
async def test_a_geography_refuses_planar_relations_and_aggregates(db_gis):
    await GisCity.objects.create(id=1, name="Moscow", center=MOSCOW)
    with pytest.raises(UnSupportedError, match="not a geography"):
        await GisCity.objects.filter(center__within=Polygon(SQUARE)).count()
    with pytest.raises(UnSupportedError, match="not a geography"):
        await GisCity.objects.aggregate(points=Collect("center"))


@pytest.mark.asyncio
async def test_a_projected_srid(db_gis):
    await GisProjectedSite.objects.create(id=1, position=Point(100, 200, srid=3857))
    await GisProjectedSite.objects.create(id=2, position="POINT(150 200)")
    site = await GisProjectedSite.objects.get(id=2)
    assert site.position == Point(150, 200, srid=3857)
    lon_lat = Point(0.0008983152841195215, 0.0017966305681926, srid=4326)
    near = await GisProjectedSite.objects.filter(position__dwithin=(lon_lat, 1)).values_list("id", flat=True)
    assert near == [1]


@pytest.mark.asyncio
async def test_wrong_lookup_values_are_refused(db_gis):
    with pytest.raises(ValidationError, match=r"\(geometry, distance\) pair"):
        await get_ids(location__dwithin=Point(0, 0))
    with pytest.raises(ValidationError, match="finite number of zero or more"):
        await get_ids(location__dwithin=(Point(0, 0), -1))
    with pytest.raises(ValidationError, match="relate pattern"):
        await get_ids(area__relate=(Point(0, 0), "T**"))
    with pytest.raises(ValidationError, match="expected a geometry"):
        await get_ids(location__intersects=5)


def test_inspectdb_maps_postgis_columns():
    def map_type(full_type: str) -> tuple[str, dict, bool]:
        udt_name = full_type.partition("(")[0]
        column = ColumnInfo(
            name="value",
            db_type="USER-DEFINED",
            nullable=True,
            is_pk=False,
            is_unique=False,
            udt_name=udt_name,
            full_type=full_type,
        )
        return ColumnTypeMapper.map_column_type(DialectName.POSTGRESQL, column)

    assert map_type("geometry(Point,4326)") == ("hare.gis.fields.PointField", {}, False)
    assert map_type("geometry(PolygonZ,3857)") == (
        "hare.gis.fields.PolygonField",
        {"srid": 3857, "dimensions": 3},
        False,
    )
    assert map_type("geography(MultiPolygon,4326)") == (
        "hare.gis.fields.MultiPolygonField",
        {"geography": True},
        False,
    )
    assert map_type("geometry(Geometry,4326)") == ("hare.gis.fields.GeometryField", {}, False)
    assert map_type("geography(Point,4326)") == (
        "hare.dialects.postgresql.fields.postgis_field.PostGISField",
        {},
        False,
    )
    assert map_type("geometry(PointM,4326)")[2] is True
    assert map_type("geometry")[2] is True
