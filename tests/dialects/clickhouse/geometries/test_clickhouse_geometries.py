"""Geometries on ClickHouse - points, lines and areas as its geo types: written and read back on both
drivers (a polygon's rings in the orientation ClickHouse's functions read), the spatial lookups and
functions ClickHouse has a function of, the others refused."""

import math

import pytest
import pytest_asyncio

from hare.exceptions import UnSupportedError, ValidationError
from hare.gis import LineString, MultiLineString, MultiPolygon, Point, Polygon
from hare.gis.functions import Area, AsText, Buffer, Distance, Intersection, Perimeter
from hare.query.functions import ArrayAgg, Count, Max
from tests.dialects.clickhouse.geometries.models import Place, Visit

#: A square of side 10 with a hole of side 2 in its middle - drawn counter-clockwise.
SQUARE = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], [[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]], srid=3857)
#: One degree of longitude by one of latitude at the equator, as a geography.
DEGREE = MultiPolygon([[[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]]], srid=4326)
#: The geometries of a place far from the others - a ClickHouse geometry holds no NULL.
FAR_AREA = Polygon([(100, 100), (100, 101), (101, 101), (101, 100), (100, 100)], srid=3857)
FAR_ZONES = MultiPolygon([[[(50, 50), (50, 51), (51, 51), (51, 50), (50, 50)]]], srid=4326)
FAR_ROUTE = LineString([(100, 100), (101, 101)], srid=3857)
FAR_ROUTES = MultiLineString([[(100, 100), (101, 101)]], srid=3857)


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
            Place(id=2, location=Point(3, 0, srid=4326), spot=Point(5, 5, srid=3857), **far_geometries()),
        ]
    )
    await Place.objects.create(id=3, location=Point(0, 0, srid=4326), spot=Point(20, 5, srid=3857), **far_geometries())


def far_geometries():
    return {"area": FAR_AREA, "zones": FAR_ZONES, "route": FAR_ROUTE, "routes": FAR_ROUTES}


async def ids(**filters):
    return await Place.objects.filter(**filters).order_by("id").values_list("id", flat=True)


@pytest.mark.database_independent
def test_column_types():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
    from hare.gis.fields import GeometryField, LineStringField, MultiPointField, PointField, PolygonField

    fields_map = Place._meta.fields_map
    assert [
        fields_map[name].get_column_type(CLICKHOUSE_DIALECT)
        for name in ("location", "area", "zones", "route", "routes")
    ] == ["Point", "Polygon", "MultiPolygon", "LineString", "MultiLineString"]
    # No ClickHouse type holds a geometry of any type, points or a z.
    for field in (GeometryField(), MultiPointField(), GeometryField("POINT", dimensions=3)):
        assert not field.exists_on(CLICKHOUSE_DIALECT)
    # A ClickHouse geometry is a tuple or an array, which holds no NULL.
    for field in (PointField(null=True), PolygonField(null=True), LineStringField(null=True)):
        with pytest.raises(UnSupportedError, match="holds no NULL"):
            field.get_column_type(CLICKHOUSE_DIALECT)


@pytest.mark.database_independent
def test_a_field_clickhouse_refuses_has_no_clickhouse_type_among_its_column_types():
    """The migrations compare a field by its type on every dialect - a nullable point, which ClickHouse
    refuses, failed every makemigrations and drift check on PostgreSQL and SQLite."""
    from hare.fields import ArrayField, IntField, MapField, TextField, TupleField
    from hare.gis.fields import PointField
    from hare.migrations.autodetection.state_signatures import StateSignatures

    for field in (
        PointField(null=True),
        TupleField([IntField(), IntField()], null=True),
        ArrayField(PointField(null=True)),
        MapField(TextField(), PointField(null=True)),
        TupleField([IntField(), PointField(null=True)]),
    ):
        assert "clickhouse" not in field.get_db_field_types()
        assert "clickhouse" not in StateSignatures.get_field_signature(field)["db_field_types"]


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
    second = await Place.objects.get(id=2)
    assert (second.route, second.routes) == (FAR_ROUTE, FAR_ROUTES)


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
async def test_an_equality_with_a_geometry_of_another_srid_is_refused(places):
    """The SRID was dropped - Point(0.5, 0.5) in 3857 matched the 4326 point as if it were one."""
    assert await ids(location=Point(0.5, 0.5, srid=4326)) == [1]
    with pytest.raises(UnSupportedError, match="give it in SRID 4326, not 3857"):
        await ids(location=Point(0.5, 0.5, srid=3857))


@pytest.mark.asyncio
async def test_an_equality_with_a_geometry_of_another_type_is_refused(places):
    """A point compared with a polygon column reached the server, which refused comparing an array with a
    tuple - a write of it is refused before."""
    with pytest.raises(ValidationError, match="area: expected a POLYGON, got a POINT"):
        await ids(area=Point(0.5, 0.5, srid=3857))
    with pytest.raises(ValidationError, match="zones: expected a MULTIPOLYGON, got a POLYGON"):
        await ids(zones=Polygon([(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)], srid=4326))


@pytest.mark.asyncio
async def test_relations_of_areas(places):
    inner = Polygon([(1, 1), (2, 1), (2, 2), (1, 1)], srid=3857)
    assert await ids(area__contains=inner) == [1]
    assert await ids(area__intersects=Polygon([(9, 9), (12, 9), (12, 12), (9, 9)], srid=3857)) == [1]
    assert await ids(area__disjoint=Polygon([(20, 20), (21, 20), (21, 21), (20, 20)], srid=3857)) == [1, 2, 3]
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
async def test_the_distances_of_areas(places):
    far = Point(20, 20, srid=3857)
    assert await ids(area__distance_lte=(far, 100)) == [1]
    assert await ids(area__distance_gt=(far, 1)) == [1, 2, 3]
    distances = (
        await Place.objects.annotate(distance=Distance("area", far))
        .order_by("distance", "id")
        .values_list("id", "distance")
    )
    assert [place_id for place_id, _distance in distances] == [1, 2, 3]
    assert math.isclose(distances[0][1], math.hypot(10, 10))
    zone_distances = (
        await Place.objects.order_by("id")
        .annotate(distance=Distance("zones", Point(0.5, 0.5, srid=4326)))
        .values_list("distance", flat=True)
    )
    assert zone_distances[0] == 0 and zone_distances[1] == zone_distances[2] > 0


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
        # Two lines meet where ClickHouse has no test of - the same coordinates aren't their intersection.
        {"route__intersects": LineString([(0, 1), (1, 0)], srid=3857)},
        {"route__equals": LineString([(0, 0), (3, 4)], srid=3857)},
        {"route__disjoint": LineString([(0, 1), (1, 0)], srid=3857)},
        {"area__touches": SQUARE},
        {"spot__within": Point(1, 1, srid=3857)},
        {"spot__within": Polygon([(0, 0), (1, 0), (1, 1), (0, 0)], srid=4326)},
    ):
        with pytest.raises(UnSupportedError):
            await ids(**filters)
    with pytest.raises(UnSupportedError):
        await Place.objects.annotate(grown=Buffer("area", 1)).values_list("grown")


@pytest_asyncio.fixture
async def visits(places):
    await Visit.objects.bulk_create(
        [
            Visit(id=1, place_id=1, stop=Point(1, 1, srid=3857), marks=[3, 4]),
            Visit(id=2, place_id=1, stop=Point(8, 8, srid=3857), marks=[5]),
        ]
    )


@pytest.mark.asyncio
async def test_a_place_without_visits_matches_no_point_lookup_of_its_missing_visit(visits):
    """A LEFT JOIN that found no visit read its point as (0, 0) - which lies inside the square, so
    the place with no visits matched; the other way round, its "no visit" didn't match isnull."""
    square = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], srid=3857)
    assert await Place.objects.filter(visits__stop__within=square).distinct().order_by("id").values_list(
        "id", flat=True
    ) == [1]
    assert await Place.objects.exclude(visits__stop__within=square).order_by("id").values_list("id", flat=True) == [
        2,
        3,
    ]
    assert await Place.objects.filter(visits__stop__isnull=True).order_by("id").values_list("id", flat=True) == [2, 3]


@pytest.mark.asyncio
async def test_aggregates_through_a_relation_leave_a_missing_row_out(visits):
    """Count, Max and ArrayAgg of an array of a visit that isn't there counted its empty value."""
    rows = (
        await Place.objects.annotate(
            visit_count=Count("visits__marks"), top_marks=Max("visits__marks"), all_marks=ArrayAgg("visits__marks")
        )
        .order_by("id")
        .values_list("id", "visit_count", "top_marks", "all_marks")
    )
    assert [(row[0], row[1], row[2], sorted(row[3])) for row in rows] == [
        (1, 2, [5], [[3, 4], [5]]),
        (2, 0, [], []),
        (3, 0, [], []),
    ]


@pytest.mark.asyncio
async def test_a_model_of_a_geometry_clickhouse_has_no_column_of_is_refused_when_bound():
    """makemigrations wrote the field and every migrate failed before its SQL - the migration chain stuck."""
    import os

    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    import sys
    import types

    from hare import fields
    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.gis.fields import GeometryField
    from hare.models import Model

    class AnyShape(Model):
        id = fields.IntField(primary_key=True, generated=False)
        shape = GeometryField()

    module_name = "tests._clickhouse_any_shape_models"
    module = types.ModuleType(module_name)
    module.AnyShape = AnyShape  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        with pytest.raises(UnSupportedError, match="AnyShape.shape. has no column on the clickhouse database"):
            async with hare_test_context([module_name], db_url=os.environ["HARE_TEST_DB"]):
                pass
    finally:
        sys.modules.pop(module_name, None)
