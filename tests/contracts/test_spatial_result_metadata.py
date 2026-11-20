"""Spatial composition preserves SRID, dimensions, geography and every operand's type."""

import math
import sys
from types import ModuleType
from urllib.parse import urlencode

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.core.hare_context import HareContext
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.gis import Point, PointField
from hare.gis.enums import GeometryType
from hare.gis.functions import Buffer, Centroid, Distance, DWithin, PointOnSurface, Transform
from hare.gis.readers import WktReader
from hare.query.expressions import Case, F, Value, When
from hare.query.functions import Coalesce
from tests.spatialite.spatialite_settings import SpatialiteTestSettings
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture
async def spatial_model(monkeypatch):
    clickhouse = DatabaseUnderTest.get_dialect().name == "clickhouse"

    class SpatialRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        point = PointField(srid=3857)
        other = PointField(srid=3857)
        geodetic = PointField(srid=4326)
        copied = fields.GeneratedField(expression=F("point"), output_field=PointField(srid=3857))
        if clickhouse:
            points = fields.ArrayField(PointField(srid=3857))
        else:
            earth = PointField(geography=True)
            copied_earth = fields.GeneratedField(expression=F("earth"), output_field=PointField(geography=True))

    module = ModuleType("tests._spatial_result_metadata")
    module.__models__ = [SpatialRecord]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    url = DatabaseUnderTest.get_url()
    if DatabaseUnderTest.get_dialect().name == "sqlite":
        options = SpatialiteTestSettings.get_options(metadata=SpatialiteMetadata.FULL)
        url += ("&" if "?" in url else "?") + urlencode(options)
    async with hare_test_context([module.__name__], url):
        await SpatialRecord.objects.create(
            id=1,
            point=Point(0, 0, srid=3857),
            other=Point(3, 4, srid=3857),
            geodetic=Point(1, 0, srid=4326),
            **({"points": [Point(3, 4, srid=3857)]} if clickhouse else {"earth": Point(0, 0, srid=4326)}),
        )
        yield SpatialRecord


@pytest.mark.asyncio
async def test_each_spatial_operand_keeps_its_geometry_type(spatial_model):
    expressions = [Distance("point", F("other")), Distance("copied", Point(3, 4, srid=3857))]
    if "points" in HareContext.require_current().apps.get_binding(spatial_model).fields_map:
        expressions.append(Distance("point", F("points__0")))
    for expression in expressions:
        assert await spatial_model.objects.annotate(result=expression).values_list("result", flat=True) == [5.0]
    if "points" in HareContext.require_current().apps.get_binding(spatial_model).fields_map:
        for index, expected in ((-1, 5.0), (5, 0.0), (-5, 0.0)):
            assert await spatial_model.objects.annotate(result=Distance("point", F(f"points__{index}"))).values_list(
                "result", flat=True
            ) == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("srid", (3857, 3395))
async def test_projected_result_metadata_controls_nested_spatial_arguments(spatial_model, srid):
    if DatabaseUnderTest.get_dialect().name == "clickhouse":
        pytest.skip("ClickHouse does not support coordinate transformation")
    expected = 6378137 * math.pi / 180 - 1000
    for repeat in range(2):
        query = spatial_model.objects.annotate(projected=Transform("geodetic", srid))
        for expression in (Transform("geodetic", srid), F("projected")):
            row = await query.annotate(
                distance=Distance(expression, Point(1000, 0, srid=srid)),
                nearby=DWithin(expression, Point(1000, 0, srid=srid), expected + 1),
            ).values("projected", "distance", "nearby")
            assert row[0]["projected"].srid == srid
            assert math.isclose(row[0]["distance"], expected, rel_tol=1e-12), repeat
            assert row[0]["nearby"] is True


@pytest.mark.asyncio
async def test_geometry_literals_preserve_srid_for_nested_operations(spatial_model):
    for srid in (3857, 3395):
        expression = Distance(Value(Point(1000, 0, srid=srid)), Point(1003, 4, srid=srid))
        assert await spatial_model.objects.annotate(result=expression).values_list("result", flat=True) == [5.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("function", [Distance, DWithin])
@pytest.mark.parametrize("wrapped", [False, True], ids=["literal", "value-expression"])
async def test_spatial_parameters_rebind_the_dialects_final_input(spatial_model, function, wrapped):
    for coordinates, expected_distance in (((3, 4), 5.0), ((6, 8), 10.0), ((3, 4), 5.0)):
        geometry = Point(*coordinates, srid=3857)
        argument = Value(geometry) if wrapped else geometry
        expression = function("point", argument, 6) if function is DWithin else function("point", argument)
        expected = expected_distance <= 6 if function is DWithin else expected_distance
        query = spatial_model.objects.annotate(result=expression)
        assert await query.values_list("result", flat=True) == [expected]
        assert await query.values("result") == [{"result": expected}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "geometry_type"),
    [
        (Buffer("point", 1), GeometryType.POLYGON),
        (Centroid("point"), GeometryType.POINT),
    ],
)
async def test_geometry_functions_read_their_result_shape(spatial_model, expression, geometry_type):
    if DatabaseUnderTest.get_dialect().name == "clickhouse":
        pytest.skip("ClickHouse does not implement these geometry-producing functions")
    for _ in range(2):
        query = spatial_model.objects.annotate(result=expression)
        result = await query.values_list("result", flat=True)
        assert result[0].GEOMETRY_TYPE is geometry_type
        assert result[0].srid == 3857
        assert not result[0].has_z
        assert await query.filter(result=result[0]).values_list("id", flat=True) == [1]
    field = HareContext.require_current().apps.get_binding(spatial_model).fields_map["point"]
    assert field.get_geometry_type() is GeometryType.POINT
    assert field.dimensions == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wkt",
    [
        "POINT Z (1 1 3)",
        "POLYGON Z ((0 0 3,2 0 3,2 2 3,0 2 3,0 0 3))",
    ],
)
async def test_centroid_lookups_use_the_result_shape_and_dimensions(spatial_model, wkt):
    if DatabaseUnderTest.get_dialect().name == "clickhouse":
        pytest.skip("ClickHouse does not implement centroid")
    geometry = WktReader.read(f"SRID=3857;{wkt}")
    query = spatial_model.objects.annotate(result=Centroid(Value(geometry)))
    result = await query.values_list("result", flat=True)
    assert result == [Point(1, 1, srid=3857)]
    assert not result[0].has_z
    assert await query.filter(result=Point(1, 1, srid=3857)).values_list("id", flat=True) == [1]
    assert geometry.has_z


@pytest.mark.asyncio
@pytest.mark.parametrize("centroid", [False, True], ids=["value", "centroid"])
async def test_geometry_result_lookups_preserve_unknown_reference_system(spatial_model, centroid):
    if DatabaseUnderTest.get_dialect().name == "clickhouse":
        pytest.skip("ClickHouse has no native SRID storage or centroid")
    for srid in (None, 4326, None):
        geometry = Point(1, 2, srid=srid)
        expression = Centroid(Value(geometry)) if centroid else Value(geometry)
        query = spatial_model.objects.annotate(result=expression)
        result = await query.values_list("result", flat=True)
        assert result == [geometry]
        for argument in (geometry, result[0], Value(geometry), F("result")):
            assert await query.filter(result=argument).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["case-then", "case-default", "coalesce-literal", "coalesce-function"])
async def test_spatial_result_literal_casts_do_not_narrow_variable_dimensions(spatial_model, operation):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("PostGIS PointOnSurface preserves line Z coordinates")
    geometry = WktReader.read("SRID=4326;LINESTRING Z (0 0 7,1 1 7)")
    function = PointOnSurface(Value(geometry))
    literal = Point(3, 4, 9, srid=4326)
    if operation == "case-then":
        expression = Case(When(id=1, then=literal), default=function)
    elif operation == "case-default":
        expression = Case(When(id=0, then=function), default=literal)
    elif operation == "coalesce-literal":
        expression = Coalesce(Value(literal), function)
    else:
        expression = Coalesce(function, Value(literal))
    query = spatial_model.objects.annotate(result=expression)
    result = (await query.values_list("result", flat=True))[0]
    assert result.has_z and result.srid == 4326
    if operation == "coalesce-function":
        x, y, z = result.get_coordinates()
        assert 0 <= x <= 1 and x == y and z == 7
    else:
        assert result == literal
    assert await query.filter(result=result).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_generated_geography_retains_measurement_units(spatial_model):
    if "copied_earth" not in HareContext.require_current().apps.get_binding(spatial_model).fields_map:
        pytest.skip("ClickHouse does not support geography fields")
    expected = 6378137 * math.pi / 180
    distance = await spatial_model.objects.annotate(
        distance=Distance("copied_earth", Point(1, 0, srid=4326))
    ).values_list("distance", flat=True)
    assert math.isclose(distance[0], expected, rel_tol=1e-10)


@pytest.mark.asyncio
@pytest.mark.parametrize("geography", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_geometry_array_defaults_keep_every_element(monkeypatch, geography, nested, count):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("Typed geometry array defaults use PostgreSQL array column types")
    points = [Point(index, index + 1, srid=4326) for index in range(count)]
    base_field = PointField(geography=geography)
    if nested:
        base_field = fields.ArrayField(base_field)
    default = [points] if nested and points else points

    class ArrayDefaultRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        points = fields.ArrayField(base_field, db_default=default)

    module = ModuleType("tests._geometry_array_defaults")
    module.__models__ = [ArrayDefaultRecord]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        for key in (1, 2):
            obj = await ArrayDefaultRecord.objects.create(id=key)
            assert obj.points == default
        assert await ArrayDefaultRecord.objects.order_by("id").values_list("points", flat=True) == [default, default]
