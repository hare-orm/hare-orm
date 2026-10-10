"""SpatiaLite's spatial index on SQLite: the spatial metadata hare creates, a SpatialiteIndex created,
kept in step with its table through migrations and dropped, the spatial lookups narrowed through it,
and the index read back by drift and inspectdb. Set HARE_TEST_SPATIALITE_PATH to run them."""

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.dialects.sqlite.indexes import SpatialiteIndex
from hare.dialects.sqlite.spatial.spatialite_blob_writer import SpatialiteBlobWriter
from hare.exceptions import ConfigurationError, DatabaseError, OperationalError, UnSupportedError
from hare.fields import CharField, IntField, UUIDField
from hare.gis import Point, PointField, Polygon
from hare.gis.functions import Distance
from hare.inspectdb import SchemaInspector
from hare.migrations.drift import detect_drift
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RemoveIndex,
    RenameField,
    RenameModel,
)
from tests.dialects.sqlite.models_spatialite_index import IndexedCity, IndexedOwner, IndexedRegion, IndexedShop
from tests.dialects.sqlite.test_full_text_migrations import MigrationRunner
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.spatialite.spatialite_settings import SpatialiteTestSettings

MODULES = ["tests.dialects.sqlite.models_spatialite_index"]
TABLE = "spatial_shop"
#: The square the middle three of the shops at (1, 1) ... (5, 5) lie in.
MIDDLE_SQUARE = Polygon([(1.5, 1.5), (4.5, 1.5), (4.5, 4.5), (1.5, 4.5), (1.5, 1.5)])
#: An SRID the WGS84 metadata hasn't - NAD83 longitude/latitude.
NAD83_SRID = 4269


@pytest_asyncio.fixture
async def indexed_db():
    async with hare_test_context(
        MODULES, db_url=SpatialiteTestSettings.get_db_url(), app_label="models", connection_label="models"
    ) as context:
        yield context


async def open_client(metadata: SpatialiteMetadata = SpatialiteMetadata.WGS84) -> AiosqliteClient:
    client = AiosqliteClient(
        file_path=":memory:",
        connection_alias=f"spatial_index_{metadata.value.lower()}",
        **SpatialiteTestSettings.get_options(metadata=metadata),
    )
    await client.create_connection(with_db=True)
    return client


@pytest_asyncio.fixture
async def runner():
    client = await open_client()
    try:
        yield MigrationRunner(client)
    finally:
        await client.close()


class SpatialSchema:
    """What SpatiaLite keeps of the spatial indexes of a database."""

    def __init__(self, client: AiosqliteClient) -> None:
        self.client = client

    async def get_index_tables(self) -> set[str]:
        rows = await self.client.execute_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'idx_%' AND sql LIKE 'CREATE VIRTUAL%'"
        )
        return {row["name"] for row in rows}

    async def get_registered_columns(self) -> set[tuple[str, str, int]]:
        rows = await self.client.execute_dicts(
            "SELECT f_table_name, f_geometry_column, spatial_index_enabled FROM geometry_columns"
        )
        return {(row["f_table_name"], row["f_geometry_column"], row["spatial_index_enabled"]) for row in rows}

    async def get_triggers(self, table: str) -> set[str]:
        rows = await self.client.execute_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", [table]
        )
        return {row["name"] for row in rows}

    async def count_index_rows(self, index_table: str) -> int:
        rows = await self.client.execute_dicts(f'SELECT count(*) AS total FROM "{index_table}"')  # nosec B608
        return rows[0]["total"]

    async def insert_shop(self, table: str, shop_id: int, point: Point, column: str = "location") -> None:
        await self.client.execute(
            f'INSERT INTO "{table}" ("id", "name", "{column}") VALUES (?, ?, ?)',  # nosec B608
            [shop_id, f"shop {shop_id}", SpatialiteBlobWriter.write(point)],
        )

    async def get_shops_within(self, table: str, column: str = "location") -> list[int]:
        rows = await self.client.execute_dicts(
            f'SELECT "id" FROM "{table}" WHERE "id" IN (SELECT ROWID FROM SpatialIndex '  # nosec B608
            f"WHERE f_table_name = ? AND f_geometry_column = ? AND search_frame = ?) ORDER BY 1",
            [table, column, SpatialiteBlobWriter.write(MIDDLE_SQUARE.with_srid(4326))],
        )
        return [row["id"] for row in rows]


def shop_fields(srid: int = 4326):
    return [
        ("id", IntField(primary_key=True)),
        ("name", CharField(max_length=50)),
        ("location", PointField(srid=srid)),
    ]


async def create_shops(runner: MigrationRunner, indexes=(), srid: int = 4326) -> SpatialSchema:
    await runner.run(
        CreateModel(name="Shop", fields=shop_fields(srid), options={"table": TABLE, "indexes": list(indexes)})
    )
    schema = SpatialSchema(runner.client)
    for shop_id in range(1, 6):
        await schema.insert_shop(TABLE, shop_id, Point(shop_id, shop_id, srid=srid))
    return schema


async def get_shop_ids(**kwargs) -> list[int]:
    return list(await IndexedShop.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))


async def create_indexed_shops() -> None:
    owner = await IndexedOwner.objects.create(id=1, name="owner")
    for shop_id in range(1, 6):
        await IndexedShop.objects.create(
            id=shop_id, name=f"shop {shop_id}", location=Point(shop_id, shop_id), owner=owner if shop_id == 3 else None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "has_metadata", "has_web_mercator"),
    [
        (SpatialiteMetadata.WGS84, True, False),
        (SpatialiteMetadata.FULL, True, True),
        (SpatialiteMetadata.NONE, False, False),
    ],
    ids=["wgs84", "full", "none"],
)
async def test_the_spatial_metadata_hare_creates(metadata, has_metadata, has_web_mercator):
    client = await open_client(metadata)
    try:
        features = client.features
        assert features.supports_spatial
        assert features.supports_geography is has_metadata
        assert features.supports_spatial_index is has_metadata
        if not has_metadata:
            assert features.spatial_reference_ids is None
            rows = await client.execute_dicts("SELECT CheckSpatialMetaData() AS layout")
            assert rows[0]["layout"] == 0
            return
        assert 4326 in features.spatial_reference_ids
        assert (3857 in features.spatial_reference_ids) is has_web_mercator
    finally:
        await client.close()


def test_the_spatial_metadata_setting_is_checked():
    with pytest.raises(ConfigurationError, match="spatialite_metadata"):
        AiosqliteClient(
            connection_alias="spatial", file_path=":memory:", load_spatialite=True, spatialite_metadata="everything"
        )
    with pytest.raises(ConfigurationError, match="only load_spatialite=True"):
        AiosqliteClient(connection_alias="spatial", file_path=":memory:", spatialite_metadata="full")


@pytest.mark.asyncio
async def test_the_generated_schema_registers_and_indexes_the_columns(indexed_db):
    schema = SpatialSchema(Connections.get("models"))
    assert await schema.get_index_tables() == {
        "idx_indexed_shop_location",
        "idx_indexed_shop_area",
        "idx_indexed_city_center",
    }
    assert await schema.get_registered_columns() == {
        ("indexed_shop", "location", 1),
        ("indexed_shop", "area", 1),
        ("indexed_city", "center", 1),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "value", "expected_ids"),
    [
        ("location__within", MIDDLE_SQUARE, [2, 3, 4]),
        ("location__intersects", MIDDLE_SQUARE, [2, 3, 4]),
        ("location__covered_by", MIDDLE_SQUARE, [2, 3, 4]),
        ("location__bbox_overlaps", MIDDLE_SQUARE, [2, 3, 4]),
        ("location__bbox_contained", MIDDLE_SQUARE, [2, 3, 4]),
        ("location__equals", Point(3, 3), [3]),
        ("location__dwithin", (Point(3, 3), 1.5), [2, 3, 4]),
    ],
)
async def test_a_spatial_lookup_goes_through_the_index(indexed_db, lookup, value, expected_ids):
    await create_indexed_shops()
    queryset = IndexedShop.objects.filter(**{lookup: value})
    assert "SpatialIndex" in queryset.sql()
    assert await get_shop_ids(**{lookup: value}) == expected_ids
    excluded = IndexedShop.objects.exclude(**{lookup: value}).order_by("id").values_list("id", flat=True)
    assert await excluded == [shop_id for shop_id in range(1, 6) if shop_id not in expected_ids]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "value", "expected_ids"),
    [
        ("location__disjoint", MIDDLE_SQUARE, [1, 5]),
        ("location__relate", (MIDDLE_SQUARE, "0FFFFF212"), [2, 3, 4]),
        ("location__distance_lte", (Point(3, 3), 1.5), [2, 3, 4]),
    ],
)
async def test_a_lookup_the_index_cant_narrow_reads_every_row(indexed_db, lookup, value, expected_ids):
    await create_indexed_shops()
    assert "SpatialIndex" not in IndexedShop.objects.filter(**{lookup: value}).sql()
    assert await get_shop_ids(**{lookup: value}) == expected_ids


@pytest.mark.asyncio
async def test_the_index_follows_writes(indexed_db):
    await create_indexed_shops()
    await IndexedShop.objects.filter(id=1).update(location=Point(3, 2))
    await IndexedShop.objects.filter(id=2).delete()
    await IndexedShop.objects.create(id=6, name="shop 6", location=Point(2, 4))
    assert await get_shop_ids(location__within=MIDDLE_SQUARE) == [1, 3, 4, 6]


@pytest.mark.asyncio
async def test_a_lookup_through_a_relation_goes_through_the_index(indexed_db):
    await create_indexed_shops()
    owners = IndexedOwner.objects.filter(shops__location__within=MIDDLE_SQUARE)
    assert "SpatialIndex" in owners.sql()
    assert await owners.values_list("id", flat=True) == [1]
    assert await IndexedOwner.objects.filter(shops__location__within=Polygon([(9, 9), (9, 8), (8, 8), (9, 9)])) == []


@pytest.mark.asyncio
async def test_a_geography_distance_reads_every_row(indexed_db):
    await IndexedCity.objects.create(id=1, center=Point(37.6173, 55.7558))
    await IndexedCity.objects.create(id=2, center=Point(30.3351, 59.9343))
    nearby = IndexedCity.objects.filter(center__dwithin=(Point(37.6, 55.7), 10_000))
    assert "SpatialIndex" not in nearby.sql()
    assert await nearby.values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_the_column_refuses_a_geometry_of_another_srid(indexed_db):
    schema = SpatialSchema(Connections.get("models"))
    with pytest.raises(DatabaseError):
        await schema.insert_shop("indexed_shop", 9, Point(1, 1, srid=3857))


@pytest.mark.asyncio
async def test_a_geography_in_a_reference_system_the_metadata_hasnt_is_refused(indexed_db):
    assert NAD83_SRID not in Connections.get("models").features.spatial_reference_ids
    await IndexedRegion.objects.create(id=1, center=Point(-77, 39))
    with pytest.raises(UnSupportedError, match=f"has no reference system {NAD83_SRID}"):
        await IndexedRegion.objects.filter(center__dwithin=(Point(-77, 39), 1000)).count()
    with pytest.raises(UnSupportedError, match=f"has no reference system {NAD83_SRID}"):
        await IndexedRegion.objects.annotate(d=Distance("center", Point(-76, 39))).values_list("d", flat=True)
    # A planar lookup measures nothing on the ellipsoid.
    assert await IndexedRegion.objects.filter(center__x=-77).count() == 1


@pytest.mark.asyncio
async def test_create_model_registers_and_fills_the_index(runner):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    assert await schema.get_index_tables() == {"idx_spatial_shop_location"}
    assert await schema.get_registered_columns() == {(TABLE, "location", 1)}
    assert await schema.count_index_rows("idx_spatial_shop_location") == 5
    assert await schema.get_shops_within(TABLE) == [2, 3, 4]


@pytest.mark.asyncio
async def test_add_and_remove_index(runner):
    schema = await create_shops(runner)
    triggers = await schema.get_triggers(TABLE)
    await runner.run(AddIndex("Shop", SpatialiteIndex(fields=("location",))))
    assert await schema.count_index_rows("idx_spatial_shop_location") == 5
    assert await schema.get_shops_within(TABLE) == [2, 3, 4]
    await runner.run(RemoveIndex("Shop", fields=["location"]))
    assert await schema.get_index_tables() == set()
    assert await schema.get_registered_columns() == set()
    assert await schema.get_triggers(TABLE) == triggers


@pytest.mark.asyncio
async def test_a_row_the_column_cant_be_registered_with_fails_the_migration(runner):
    schema = await create_shops(runner)
    await schema.insert_shop(TABLE, 9, Point(1, 1, srid=3857))
    with pytest.raises(OperationalError, match="refused to register the column"):
        await runner.run(AddIndex("Shop", SpatialiteIndex(fields=("location",))))


@pytest.mark.asyncio
async def test_an_srid_the_metadata_hasnt_is_refused_before_any_sql(runner):
    with pytest.raises(UnSupportedError, match="has no reference system 3857"):
        await create_shops(runner, [SpatialiteIndex(fields=("location",))], srid=3857)
    client = await open_client(SpatialiteMetadata.FULL)
    try:
        schema = await create_shops(MigrationRunner(client), [SpatialiteIndex(fields=("location",))], srid=3857)
        assert await schema.get_registered_columns() == {(TABLE, "location", 1)}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_renaming_the_indexed_field_moves_the_index(runner):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    await runner.run(RenameField("Shop", "location", "place"))
    assert await schema.get_index_tables() == {"idx_spatial_shop_place"}
    assert await schema.get_registered_columns() == {(TABLE, "place", 1)}
    await schema.insert_shop(TABLE, 6, Point(3, 2, srid=4326), column="place")
    assert await schema.get_shops_within(TABLE, "place") == [2, 3, 4, 6]


@pytest.mark.asyncio
async def test_altering_the_indexed_field_column_moves_the_index(runner):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    await runner.run(AlterField("Shop", "location", PointField(source_field="position")))
    assert await schema.get_index_tables() == {"idx_spatial_shop_position"}
    assert await schema.get_registered_columns() == {(TABLE, "position", 1)}
    assert await schema.get_shops_within(TABLE, "position") == [2, 3, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", ["model", "table"])
async def test_renaming_the_table_moves_the_index(runner, rename):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    await runner.run(AlterModelTable("Shop", "spatial_store"))
    if rename == "model":
        await runner.run(RenameModel("Shop", "Store"))
    assert await schema.get_index_tables() == {"idx_spatial_store_location"}
    assert await schema.get_registered_columns() == {("spatial_store", "location", 1)}
    await schema.insert_shop("spatial_store", 6, Point(3, 2, srid=4326))
    assert await schema.get_shops_within("spatial_store") == [2, 3, 4, 6]


@pytest.mark.asyncio
async def test_a_table_rebuild_keeps_the_index_in_step(runner):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    await runner.run(AlterField("Shop", "name", CharField(max_length=80, null=True)))
    await runner.run(AddField("Shop", "code", UUIDField(null=True)))
    assert await schema.get_registered_columns() == {(TABLE, "location", 1)}
    assert await schema.count_index_rows("idx_spatial_shop_location") == 5
    await schema.insert_shop(TABLE, 6, Point(3, 2, srid=4326))
    assert await schema.get_shops_within(TABLE) == [2, 3, 4, 6]


@pytest.mark.asyncio
async def test_delete_model_drops_the_index(runner):
    schema = await create_shops(runner, [SpatialiteIndex(fields=("location",))])
    await runner.run(DeleteModel("Shop"))
    assert await schema.get_index_tables() == set()
    assert await schema.get_registered_columns() == set()
    assert await schema.get_triggers(TABLE) == set()


@pytest.mark.asyncio
async def test_collected_sql_registers_and_indexes_the_column():
    client = await open_client()
    try:
        collecting = MigrationRunner(client, collect_sql=True)
        await collecting.run(
            CreateModel(
                name="Shop",
                fields=shop_fields(),
                options={"table": TABLE, "indexes": [SpatialiteIndex(fields=("location",))]},
            )
        )
        collected_sql = "\n".join(collecting.editor.collected_sql)
        assert "RecoverGeometryColumn('spatial_shop', 'location', 4326, 'POINT', 'XY')" in collected_sql
        assert "CreateSpatialIndex('spatial_shop', 'location')" in collected_sql
        assert await SpatialSchema(client).get_index_tables() == set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_model_without_an_integer_primary_key_is_refused(runner):
    with pytest.raises(ConfigurationError, match="SpatialiteIndex needs an integer primary key"):
        await runner.run(
            CreateModel(
                name="Shop",
                fields=[("code", CharField(max_length=10, primary_key=True)), ("location", PointField())],
                options={"table": TABLE, "indexes": [SpatialiteIndex(fields=("location",))]},
            )
        )


@pytest.mark.asyncio
async def test_a_field_that_isnt_a_geometry_is_refused(runner):
    with pytest.raises(ConfigurationError, match="SpatialiteIndex indexes a GeometryField - 'name' isn't one"):
        await create_shops(runner, [SpatialiteIndex(fields=("name",))])


@pytest.mark.parametrize("fields_value", [(), ("location", "area"), "location"])
def test_the_index_takes_one_field(fields_value):
    with pytest.raises(ConfigurationError, match="exactly one geometry field"):
        SpatialiteIndex(fields=fields_value)


def test_the_index_has_no_key_order_and_no_name():
    with pytest.raises(ConfigurationError, match="no key order"):
        SpatialiteIndex(fields=("-location",))
    with pytest.raises(TypeError):
        SpatialiteIndex(fields=("location",), name="shop_location")  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def round_trip():
    async with hare_test_context(MODULES, db_url=SpatialiteTestSettings.get_db_url()) as context:
        yield RoundTrip(context.get_connection())


def build_store(indexes: list[SpatialiteIndex]) -> type:
    return build_model(
        "Store",
        "drift_store",
        {"name": fields.CharField(max_length=50), "location": PointField()},
        {"indexes": indexes},
    )


@pytest.mark.asyncio
async def test_a_migrated_index_has_no_drift_and_a_missing_one_has(round_trip):
    store = build_store([SpatialiteIndex(fields=("location",))])
    operations = await round_trip.migrate_to(store)
    assert [type(operation).__name__ for operation in operations] == ["CreateExtension", "CreateModel"]
    assert (await detect_drift(round_trip.connection, build_live_state(store), [APP_LABEL])).operations == []
    unindexed = build_store([])
    drift = await detect_drift(round_trip.connection, build_live_state(unindexed), [APP_LABEL])
    assert [type(operation).__name__ for operation in drift.operations] == ["RemoveIndex"]
    assert [type(operation).__name__ for operation in await round_trip.migrate_to(unindexed)] == ["RemoveIndex"]
    assert (await detect_drift(round_trip.connection, build_live_state(unindexed), [APP_LABEL])).operations == []


@pytest.mark.asyncio
async def test_inspectdb_reads_the_index_and_skips_its_tables_and_triggers(round_trip):
    await round_trip.migrate_to(build_store([SpatialiteIndex(fields=("location",))]))
    source = await SchemaInspector.inspect(round_trip.connection, ["drift_store"])
    assert "SpatialiteIndex(fields=['location'])" in source
    assert "Trigger(" not in source
