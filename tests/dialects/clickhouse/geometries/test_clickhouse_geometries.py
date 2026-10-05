"""Geometries on ClickHouse - points, lines and areas as its geo types: written and read back on both
drivers (a polygon's rings in the orientation ClickHouse's functions read), the spatial lookups and
functions ClickHouse has a function of, the others refused."""

import math

import pytest
import pytest_asyncio

from hare.exceptions import UnSupportedError
from hare.gis import LineString, MultiLineString, MultiPolygon, Point, Polygon
from hare.gis.functions import Area, AsText, Buffer, Distance, Intersection, Perimeter
from tests.dialects.clickhouse.geometries.models import Place

#: A square of side 10 with a hole of side 2 in its middle - drawn counter-clockwise.
SQUARE = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], [[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]], srid=3857)
#: One degree of longitude by one of latitude at the equator, as a geography.
DEGREE = MultiPolygon([[[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]]], srid=4326)


@pytest_asyncio.fixture
async def places(clickhouse_geometries_db):
    await Place.objects.bulk_create(
        [
            Place(
                id=1,
                location=Point(0.5, 0.5, srid=4326),
                spot=Point(2, 2, srid=3857),
                area=SQUARE,
                zones=DEGREE,
                route=LineString([(0, 0), (3, 4)], srid=3857),
                routes=MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]], srid=3857),
            ),
            Place(id=2, location=Point(3, 0, srid=4326), spot=Point(5, 5, srid=3857)),
        ]
    )
    await Place.objects.create(id=3, location=Point(0, 0, srid=4326), spot=Point(20, 5, srid=3857))


async def ids(**filters):
    return await Place.objects.filter(**filters).order_by("id").values_list("id", flat=True)


def test_column_types():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
    from hare.gis.fields import GeometryField, MultiPointField, PointField

    fields_map = Place._meta.fields_map
    assert [
        fields_map[name].get_column_type(CLICKHOUSE_DIALECT)
        for name in ("location", "area", "zones", "route", "routes")
    ] == ["Point", "Polygon", "MultiPolygon", "LineString", "MultiLineString"]
    # No ClickHouse type holds a geometry of any type, points or a z.
    for field in (GeometryField(), MultiPointField(), GeometryField("POINT", dimensions=3)):
        assert not field.exists_on(CLICKHOUSE_DIALECT)
    # A point has no empty value to write a NULL as.
    with pytest.raises(UnSupportedError):
        PointField(null=True).get_column_type(CLICKHOUSE_DIALECT)


@pytest.mark.asyncio
async def test_geometries_round_trip(places):
    first = await Place.objects.get(id=1)
    assert first.location == Point(0.5, 0.5, srid=4326)
    assert first.route == LineString([(0, 0), (3, 4)], srid=3857)
    assert first.routes == MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]], srid=3857)
    # The outer ring clockwise, the hole counter-clockwise - the square was drawn the other way round.
    assert first.area == Polygon(
        [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)], [[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]], srid=3857
    )
    assert first.zones == DEGREE
    # A NULL area is written as an empty one, as a container's NULL.
    second = await Place.objects.get(id=2)
    assert (second.area, second.zones, second.route) == (
        Polygon(srid=3857),
        MultiPolygon(srid=4326),
        LineString(srid=3857),
    )


@pytest.mark.asyncio
async def test_points_in_areas(places):
    assert await ids(spot__within=SQUARE) == [1]
    assert await ids(spot__intersects=SQUARE) == [1]
    # A point in the hole is outside.
    assert await ids(spot__disjoint=SQUARE) == [2, 3]
    assert await ids(area__contains=Point(2, 2, srid=3857)) == [1]
    assert await ids(area__contains=Point(5, 5, srid=3857)) == []
    assert await ids(location__within=DEGREE) == [1]
    assert await ids(zones__covers=Point(0.5, 0.5, srid=4326)) == [1]


@pytest.mark.asyncio
async def test_relations_of_areas(places):
    inner = Polygon([(1, 1), (2, 1), (2, 2), (1, 1)], srid=3857)
    assert await ids(area__contains=inner) == [1]
    assert await ids(area__intersects=Polygon([(9, 9), (12, 9), (12, 12), (9, 9)], srid=3857)) == [1]
    assert await ids(area__disjoint=Polygon([(20, 20), (21, 20), (21, 21), (20, 20)], srid=3857)) == [1]
    assert await ids(area__equals=SQUARE) == [1]
    assert await ids(area=SQUARE) == [1]


@pytest.mark.asyncio
async def test_distances(places):
    # Planar distances in the SRID's units, of points and of a point and an area.
    assert await ids(spot__dwithin=(Point(0, 0, srid=3857), 3)) == [1]
    assert await ids(spot__distance_lte=(SQUARE, 10)) == [1, 2, 3]
    assert await ids(spot__distance_gt=(SQUARE, 0)) == [2, 3]
    # A geography's distance in meters - a degree of longitude at the equator is about 111 km.
    assert await ids(location__dwithin=(Point(1, 0, srid=4326), 112_000)) == [1, 3]
    distances = (
        await Place.objects.order_by("id")
        .annotate(distance=Distance("location", Point(0, 0, srid=4326)))
        .values_list("distance", flat=True)
    )
    assert distances[2] == 0
    assert math.isclose(distances[1], 333_958, rel_tol=1e-2)


@pytest.mark.asyncio
async def test_functions(places):
    first = (
        await Place.objects.filter(id=1)
        .annotate(
            area_size=Area("area"),
            perimeter=Perimeter("area"),
            zone_size=Area("zones"),
            text=AsText("route"),
            shared=Intersection("area", Polygon([(8, 8), (12, 8), (12, 12), (8, 12), (8, 8)], srid=3857)),
        )
        .values("area_size", "perimeter", "zone_size", "text", "shared")
    )
    (row,) = first
    assert row["area_size"] == 96
    assert row["perimeter"] == 48
    assert math.isclose(row["zone_size"], 12_364_000_000, rel_tol=1e-2)
    assert row["text"] == "LINESTRING(0 0,3 4)"
    assert await Place.objects.filter(id__in=[1, 2]).order_by("id").values_list("spot__x", "spot__y") == [
        (2, 2),
        (5, 5),
    ]
    assert await Place.objects.filter(id=1).values_list("spot__srid", flat=True) == [3857]


@pytest.mark.asyncio
async def test_what_clickhouse_has_no_function_of_is_refused(places):
    for filters in (
        {"route__crosses": LineString([(0, 1), (1, 0)], srid=3857)},
        {"area__touches": SQUARE},
        {"spot__within": Point(1, 1, srid=3857)},
        {"spot__within": Polygon([(0, 0), (1, 0), (1, 1), (0, 0)], srid=4326)},
    ):
        with pytest.raises(UnSupportedError):
            await ids(**filters)
    with pytest.raises(UnSupportedError):
        await Place.objects.annotate(grown=Buffer("area", 1)).values_list("grown")
