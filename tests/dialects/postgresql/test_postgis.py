import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.gis import PostGISField
from hare.dialects.postgresql.functions.gis import STDistance, STDWithin
from hare.dialects.registry import DialectRegistry
from hare.exceptions import UnSupportedError, ValidationError
from tests.dialects.postgresql.models_postgis import Place, PlaceWithValidatedLocation


def test_sql_type_raises_unsupported_error_for_non_postgres_dialect():
    """geography(Point,4326) is a PostGIS/Postgres-only column type - resolving it for another
    dialect's DDL must raise a clear ConfigurationError instead of silently handing back Postgres
    syntax for schema generation to choke on."""
    field = PostGISField()
    field.model_field_name = "location"
    with pytest.raises(UnSupportedError, match="PostGISField.*location.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


def test_sql_type_still_resolves_for_postgres_dialect():
    field = PostGISField()
    field.model_field_name = "location"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "geography(Point,4326)"


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
