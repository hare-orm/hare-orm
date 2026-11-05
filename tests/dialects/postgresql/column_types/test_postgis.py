import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.exceptions import ValidationError
from hare.gis import Point, PointField
from hare.gis.functions import Distance, DWithin
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.query.expressions import F
from tests.dialects.postgresql.models_postgis import Place, PlaceWithValidatedLocation


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [(1.0, 2.0, 3.0), 123])
def test_a_geography_value_requires_a_geometry(value):
    with pytest.raises(ValidationError):
        PointField(geography=True).to_db_value(value, Place)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_geography_round_trip_preserves_asymmetric_coordinates(db_postgis):
    point = Point(37.6173, 55.7558, srid=4326)
    place = await Place.objects.create(name="City centre", location=point)
    reread = await Place.objects.get(id=place.id)
    assert reread.location == point
    assert reread.location.x == 37.6173
    assert reread.location.y == 55.7558


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_dwithin_lookup_and_expression_use_the_same_distance_units(db_postgis):
    near = await Place.objects.create(name="Near", location=Point(37.62, 55.75))
    await Place.objects.create(name="Far", location=Point(30.0, 50.0))
    point = Point(37.6173, 55.7558)
    via_lookup = await Place.objects.filter(location__dwithin=(point, 5000))
    via_expression = await Place.objects.annotate(within=DWithin("location", point, 5000)).filter(within=True)
    assert [obj.id for obj in via_lookup] == [near.id]
    assert [obj.id for obj in via_expression] == [near.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distance_orders_geography_by_meters(db_postgis):
    near = await Place.objects.create(name="Near", location=Point(37.6173, 55.7558))
    far = await Place.objects.create(name="Far", location=Point(30.0, 50.0))
    rows = (
        await Place.objects.annotate(distance=Distance("location", Point(37.6173, 55.7558)))
        .order_by("distance")
        .values("id", "distance")
    )
    assert [row["id"] for row in rows] == [near.id, far.id]
    assert rows[0]["distance"] < 1
    assert rows[1]["distance"] > 500_000


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["lookup", "predicate", "distance"])
async def test_spatial_query_arguments_run_field_validators(db_postgis, operation):
    await PlaceWithValidatedLocation.objects.create(name="North", location=Point(37.6173, 55.7558))
    point = Point(151.2093, -33.8688)
    with pytest.raises(ValidationError, match="northern hemisphere"):
        if operation == "lookup":
            await PlaceWithValidatedLocation.objects.filter(location__dwithin=(point, 5000))
        elif operation == "predicate":
            await PlaceWithValidatedLocation.objects.annotate(near=DWithin("location", point, 5000)).values("near")
        else:
            await PlaceWithValidatedLocation.objects.annotate(distance=Distance("location", point)).values("distance")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_extension_tables_and_views_are_not_models(db_postgis):
    connection = Connections.get("models")
    table_names = await DatabaseCatalog.get_table_names(connection)
    view_names = [name for name, _is_materialized, _triggers in await DatabaseCatalog.get_views(connection)]
    assert "spatial_ref_sys" not in table_names
    assert Place._meta.db_table in table_names
    assert not {"geometry_columns", "geography_columns"} & set(view_names)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_geography_inequality_excludes_equal_points_for_reads_and_writes(db_postgis):
    first, second = Point(2, 1), Point(4, 3)
    await Place.objects.create(id=1, name="a", location=first)
    await Place.objects.create(id=2, name="b", location=second)
    assert await Place.objects.filter(location__not=first).values_list("id", flat=True) == [2]
    assert await Place.objects.filter(location__not_in=[first]).values_list("id", flat=True) == [2]
    assert await Place.objects.filter(location__not_in=[first, second]).count() == 0
    assert await Place.objects.filter(location__not=first).update(name="c") == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("coordinates", [(0, 91), (181, 0), (0, float("nan")), ("0", "1e5"), (True, False)])
async def test_invalid_geography_coordinates_are_refused_before_storage(db_postgis, coordinates):
    with pytest.raises(ValidationError):
        await Place.objects.create(name="Invalid", location=Point(*coordinates))
    assert await Place.objects.count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("function", [Distance, DWithin])
async def test_a_later_query_validates_its_current_geography(db_postgis, function):
    await Place.objects.create(name="Valid", location=Point(5, 5))

    def build(point):
        expression = function("location", point, *([5000] if function is DWithin else []))
        return Place.objects.annotate(result=expression).values("result")

    await build(Point(5, 5))
    with pytest.raises(ValidationError, match="off the globe"):
        await build(Point(2, 91))
    with pytest.raises(ValidationError):
        await build("POINT(2 NaN)")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("radius", ["10", True, float("nan"), float("inf"), -5])
async def test_distance_predicates_reject_invalid_radii(db_postgis, radius):
    with pytest.raises(ValidationError, match="distance"):
        await Place.objects.filter(location__dwithin=(Point(2, 1), radius))
    with pytest.raises(ValidationError, match="distance"):
        DWithin("location", Point(2, 1), radius)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_dwithin_accepts_geometry_and_radius_expressions(db_postgis):
    place = await Place.objects.create(name="Here", location=Point(37.6173, 55.7558))
    result = (
        await Place.objects.filter(pk=place.pk)
        .annotate(near=DWithin("location", F("location"), F("id")))
        .values_list("near", flat=True)
    )
    assert result == [True]
