"""An index of one database's own type - a PostgreSQL access method, SQLite's full-text or spatial index - is
refused on a database of another dialect before any SQL, whether the schema is generated or a
migration adds or drops it: the model or the migration has to name an index of that database."""

import pytest
import pytest_asyncio

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.indexes import GinIndex, GistIndex, HnswIndex
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.indexes import FullTextIndex, SpatialiteIndex
from hare.exceptions import UnSupportedError
from hare.fields import CharField, IntField
from hare.gis import PointField
from hare.migrations.operations import AddIndex, CreateModel, RemoveIndex
from tests.dialects.sqlite.test_full_text_migrations import MigrationRunner

POSTGRESQL_INDEX_MESSAGE = r"GistIndex\(fields=\['name'\]\) is an index of PostgreSQL - the sqlite dialect has none"


@pytest_asyncio.fixture
async def runner():
    client = AiosqliteClient(file_path=":memory:", connection_alias="dialect_indexes")
    await client.create_connection(with_db=True)
    try:
        yield MigrationRunner(client)
    finally:
        await client.close()


def place_fields():
    return [("id", IntField(primary_key=True)), ("name", CharField(max_length=20))]


@pytest.mark.parametrize(
    "index",
    [
        GistIndex(fields=("name",)),
        GinIndex(fields=("name",)),
        HnswIndex(fields=("name",), opclasses=("vector_l2_ops",)),
    ],
)
def test_a_postgresql_index_is_refused_by_another_dialect(index):
    with pytest.raises(UnSupportedError, match="is an index of PostgreSQL - the sqlite dialect has none"):
        index.raise_if_unsupported(SQLITE_DIALECT)
    with pytest.raises(UnSupportedError, match="is an index of PostgreSQL"):
        index.raise_if_not_droppable(SQLITE_DIALECT.features, SQLITE_DIALECT)
    index.raise_if_unsupported(POSTGRESQL_DIALECT)
    index.raise_if_not_droppable(POSTGRESQL_DIALECT.features, POSTGRESQL_DIALECT)


def test_a_spatialite_index_is_refused_by_postgresql():
    index = SpatialiteIndex(fields=("location",))
    message = r"SpatialiteIndex\(fields=\['location'\]\) is an index of SQLite's SpatiaLite - the postgresql dialect"
    with pytest.raises(UnSupportedError, match=message):
        index.raise_if_unsupported(POSTGRESQL_DIALECT)
    with pytest.raises(UnSupportedError, match=message):
        index.raise_if_not_droppable(POSTGRESQL_DIALECT.features, POSTGRESQL_DIALECT)
    index.raise_if_unsupported(SQLITE_DIALECT)


@pytest.mark.asyncio
async def test_a_spatialite_index_needs_spatialite_with_its_metadata(runner):
    with pytest.raises(UnSupportedError, match="supports_spatial_index"):
        await runner.run(
            CreateModel(
                name="Place",
                fields=[*place_fields(), ("location", PointField())],
                options={"table": "dialect_place", "indexes": [SpatialiteIndex(fields=("location",))]},
            )
        )
    assert "dialect_place" not in await runner.get_schema_names()


def test_a_full_text_index_is_refused_by_postgresql():
    index = FullTextIndex(fields=("name",))
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        index.raise_if_unsupported(POSTGRESQL_DIALECT)
    with pytest.raises(UnSupportedError, match="supports_full_text_index"):
        index.raise_if_not_droppable(POSTGRESQL_DIALECT.features, POSTGRESQL_DIALECT)


@pytest.mark.asyncio
async def test_a_model_with_a_postgresql_index_is_refused_before_any_sql(runner):
    with pytest.raises(UnSupportedError, match=POSTGRESQL_INDEX_MESSAGE):
        await runner.run(
            CreateModel(
                name="Place",
                fields=place_fields(),
                options={"table": "dialect_place", "indexes": [GistIndex(fields=("name",))]},
            )
        )
    assert "dialect_place" not in await runner.get_schema_names()


@pytest.mark.asyncio
async def test_a_migration_adding_or_dropping_a_postgresql_index_is_refused(runner):
    await runner.run(CreateModel(name="Place", fields=place_fields(), options={"table": "dialect_place"}))
    with pytest.raises(UnSupportedError, match=POSTGRESQL_INDEX_MESSAGE):
        await runner.run(AddIndex("Place", GistIndex(fields=("name",), name="dialect_place_gist")))
    runner.state.models[("models", "Place")].options["indexes"] = [
        GistIndex(fields=("name",), name="dialect_place_gist")
    ]
    with pytest.raises(UnSupportedError, match=POSTGRESQL_INDEX_MESSAGE):
        await runner.run(RemoveIndex("Place", "dialect_place_gist"))
