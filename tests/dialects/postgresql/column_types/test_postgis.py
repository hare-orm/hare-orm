import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.dialects.postgresql.functions.spatial import STDistance, STDWithin
from hare.exceptions import ValidationError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from tests.dialects.postgresql.models_postgis import Place, PlaceWithValidatedLocation


@pytest.mark.database_independent
def test_to_db_value_wraps_wrong_shape():
    """PostGISField.to_db_value does `latitude, longitude = value` with no check that `value`
    actually unpacks to exactly 2 elements first - a wrong-shape value (e.g. a 3-tuple, or a
    non-iterable) raised a bare ValueError/TypeError instead of the framework's own catchable
    ValidationError, same unwrapped-exception bug class as every other field's own conversion
    step fixed this session."""
    field = PostGISField()
    with pytest.raises(ValidationError):
        field.to_db_value((1.0, 2.0, 3.0), Place)
    with pytest.raises(ValidationError):
        field.to_db_value(123, Place)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgis_round_trip(db_postgis):
    place = await Place.objects.create(name="Moscow center", location=(55.7558, 37.6173))
    reread = await Place.objects.get(id=place.id)
    assert reread.location == (55.7558, 37.6173)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgis_stdwithin_uses_gist_index(db_postgis):
    near = await Place.objects.create(name="Near", location=(55.75, 37.62))
    await Place.objects.create(name="Far", location=(50.0, 30.0))

    results = (
        await Place.objects.all().annotate(within=STDWithin("location", (55.7558, 37.6173), 5000)).filter(within=True)
    )
    assert [p.id for p in results] == [near.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgis_within_km_lookup_matches_stdwithin(db_postgis):
    near = await Place.objects.create(name="Near", location=(55.75, 37.62))
    await Place.objects.create(name="Far", location=(50.0, 30.0))

    via_lookup = await Place.objects.filter(location__within_km=(55.7558, 37.6173, 5.0))
    via_stdwithin = (
        await Place.objects.all().annotate(within=STDWithin("location", (55.7558, 37.6173), 5000)).filter(within=True)
    )
    assert [p.id for p in via_lookup] == [near.id]
    assert [p.id for p in via_lookup] == [p.id for p in via_stdwithin]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgis_stdistance_orders_by_meters(db_postgis):
    moscow = await Place.objects.create(name="Moscow center", location=(55.7558, 37.6173))
    kyiv = await Place.objects.create(name="Kyiv", location=(50.0, 30.0))

    rows = (
        await Place.objects.all()
        .annotate(dist=STDistance("location", (55.7558, 37.6173)))
        .order_by("dist")
        .values("id", "dist")
    )
    assert rows[0]["id"] == moscow.id
    assert rows[0]["dist"] < 1
    assert rows[-1]["id"] == kyiv.id
    assert rows[-1]["dist"] > 500_000


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_within_km_lookup_runs_the_field_s_own_validators_on_the_query_point(db_postgis):
    """__within_km's own value_encoder used to skip field_object.to_db_value()/validate()
    entirely for the query point - a custom validators=[...] on the field never ran for it, even
    though create()/save() already run it for a stored point."""
    await PlaceWithValidatedLocation.objects.create(name="Moscow center", location=(55.7558, 37.6173))
    with pytest.raises(ValidationError, match="northern hemisphere"):
        await PlaceWithValidatedLocation.objects.filter(location__within_km=(-33.8688, 151.2093, 5.0))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stdwithin_runs_the_field_s_own_validators_on_the_query_point(db_postgis):
    """STDWithin used to build the geography literal straight from the raw query point via
    as_geography_term(), skipping the target field's own validate() - same bug class as
    __within_km above."""
    await PlaceWithValidatedLocation.objects.create(name="Moscow center", location=(55.7558, 37.6173))
    with pytest.raises(ValidationError, match="northern hemisphere"):
        await (
            PlaceWithValidatedLocation.objects.all()
            .annotate(within=STDWithin("location", (-33.8688, 151.2093), 5000))
            .filter(within=True)
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stdistance_runs_the_field_s_own_validators_on_the_query_point(db_postgis):
    """Same bug class as the two tests above, for STDistance."""
    await PlaceWithValidatedLocation.objects.create(name="Moscow center", location=(55.7558, 37.6173))
    with pytest.raises(ValidationError, match="northern hemisphere"):
        await (
            PlaceWithValidatedLocation.objects.all()
            .annotate(dist=STDistance("location", (-33.8688, 151.2093)))
            .values("dist")
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_the_tables_and_views_of_the_extension_are_no_models(db_postgis):
    # spatial_ref_sys, geometry_columns, geography_columns are PostGIS's: drift and inspectdb skip them.
    connection = Connections.get("models")
    table_names = await DatabaseCatalog.get_table_names(connection)
    view_names = [name for name, _is_materialized, _triggers in await DatabaseCatalog.get_views(connection)]
    assert "spatial_ref_sys" not in table_names
    assert Place._meta.db_table in table_names
    assert not {"geometry_columns", "geography_columns"} & set(view_names)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_not_equal_filter_of_a_point_leaves_the_equal_row_out(db_postgis):
    """geography has an = operator and no <>: __not and __not_in compared its text on rust_pg - every row
    "not equal", an update of them too - and failed on asyncpg."""
    await Place.objects.create(id=1, name="a", location=(1, 2))
    await Place.objects.create(id=2, name="b", location=(3, 4))
    assert await Place.objects.filter(location__not=(1, 2)).values_list("id", flat=True) == [2]
    assert await Place.objects.filter(location__not_in=[(1, 2)]).values_list("id", flat=True) == [2]
    assert await Place.objects.filter(location__not_in=[(1, 2), (3, 4)]).count() == 0
    assert await Place.objects.filter(location__not=(1, 2)).update(name="c") == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "message"),
    [
        ((91, 0), "latitude 91 is off the globe"),
        ((0, 181), "longitude 181 is off the globe"),
        ((float("nan"), 0), "latitude nan is off the globe"),
        (("1e5", "0"), "latitude takes a number, got str"),
        ((True, False), "latitude takes a number, got bool"),
    ],
    ids=["latitude", "longitude", "nan", "text", "bool"],
)
async def test_a_point_off_the_globe_is_refused(db_postgis, location, message):
    """PostGIS wrapped it around the globe - (91, 0) read back as (89, 0), (0, 181) as (0, -179) - and
    stored NaN; a numeric text was parsed by the database."""
    with pytest.raises(ValidationError, match=message):
        await Place.objects.create(id=1, name="a", location=location)
    assert await Place.objects.count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        lambda point: Place.objects.annotate(dist=STDistance("location", point)).values("dist"),
        lambda point: Place.objects.annotate(near=STDWithin("location", point, 5000)).filter(near=True),
    ],
    ids=["stdistance", "stdwithin"],
)
async def test_a_query_point_off_the_globe_is_refused_by_a_reused_plan_too(db_postgis, build):
    """The point was checked only while the plan was built - after one valid query, (91, 2) wrapped
    around the globe and NaN or text measured 0 meters."""
    await Place.objects.create(id=1, name="a", location=(5, 5))
    await build((5, 5))
    for point, message in (((91, 2), "off the globe"), ((float("nan"), 2), "off the globe"), (("1", "2"), "str")):
        with pytest.raises(ValidationError, match=message):
            await build(point)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("radius", ["10", True, float("nan"), float("inf"), -5])
async def test_a_radius_that_is_no_finite_number_of_zero_or_more_is_refused(db_postgis, radius):
    """A text radius went to the database repeated a thousand times, True was 1 km, NaN, infinity and a
    negative radius ran silently - and the two drivers answered a text radius of STDWithin differently."""
    with pytest.raises(ValidationError, match="radius"):
        await Place.objects.filter(location__within_km=(1, 2, radius))
    with pytest.raises(ValidationError, match="radius"):
        STDWithin("location", (1, 2), radius)
