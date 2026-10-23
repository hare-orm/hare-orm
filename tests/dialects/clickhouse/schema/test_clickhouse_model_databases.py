"""A model moved to another database of the ClickHouse server (``Meta.schema``): its table is renamed into
that database with its rows, and back when the migration is unapplied; a model whose views name its
table by its database is refused before any statement."""

import os
import textwrap
import uuid

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


MODEL_TEMPLATE = """
from hare.ddl import View

class Visit(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=50)
    class Meta:
        {schema}
        {views}
"""


def get_models(database: str | None = None, views: bool = False) -> str:
    return textwrap.dedent(
        MODEL_TEMPLATE.format(
            schema=f"schema = {database!r}" if database else "pass",
            views='views = [View("visit_sites", query=RawSQLTerm("SELECT site FROM visit"))]' if views else "",
        )
    )


@pytest.mark.asyncio
async def test_a_table_moves_to_another_database_with_its_rows_and_back(project_factory):  # noqa: F811
    project = await project_factory()
    database = f"hare_moved_{uuid.uuid4().hex[:8]}"
    await project.query(f"CREATE DATABASE {database}")
    try:
        project.write_models(get_models())
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.query("INSERT INTO visit (id, site) VALUES (1, 'a')")

        project.write_models(get_models(database))
        await project.cli_ok("makemigrations")
        assert "AlterModelSchema" in project.read_latest_migration()
        await project.cli_ok("migrate")
        assert await project.query(f"SELECT id, site FROM {database}.visit") == [{"id": 1, "site": "a"}]
        assert await project.query(
            "SELECT count() AS tables FROM system.tables WHERE database = currentDatabase() AND name = 'visit'"
        ) == [{"tables": 0}]

        await project.cli_ok("migrate", "app", "0001_initial")
        assert await project.query("SELECT id, site FROM visit") == [{"id": 1, "site": "a"}]
    finally:
        await project.query(f"DROP DATABASE IF EXISTS {database}")


@pytest.mark.asyncio
async def test_a_model_whose_views_name_its_table_isnt_moved(project_factory):  # noqa: F811
    project = await project_factory()
    database = f"hare_moved_{uuid.uuid4().hex[:8]}"
    await project.query(f"CREATE DATABASE {database}")
    try:
        project.write_models(get_models(views=True))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.query("INSERT INTO visit (id, site) VALUES (1, 'a')")

        project.write_models(get_models(database, views=True))
        await project.cli_ok("makemigrations")
        result = await project.cli("migrate")
        assert result.exit_code not in (0, None)
        assert "remove them before moving the table to another database" in result.output
        assert await project.query("SELECT id, site FROM visit") == [{"id": 1, "site": "a"}]
    finally:
        await project.query(f"DROP DATABASE IF EXISTS {database}")


@pytest.mark.asyncio
async def test_a_model_naming_the_connections_database_stays_where_it_is(project_factory):  # noqa: F811
    """Its table was renamed into the database it already was in - TABLE_ALREADY_EXISTS."""
    project = await project_factory()
    project.write_models(get_models())
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO visit (id, site) VALUES (1, 'a')")
    (row,) = await project.query("SELECT currentDatabase() AS name")
    project.write_models(get_models(row["name"]))
    await project.cli_ok("makemigrations")

    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "0001")

    assert await project.query("SELECT site FROM visit") == [{"site": "a"}]


@pytest.mark.asyncio
async def test_drift_compares_a_model_of_another_database_in_a_shadow_of_its_own(project_factory):  # noqa: F811
    project = await project_factory()
    database = f"hare_moved_{uuid.uuid4().hex[:8]}"
    await project.query(f"CREATE DATABASE {database}")
    try:
        project.write_models(get_models(database))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        # A table of the model's name in the connection's database is not the model's.
        await project.query("CREATE TABLE visit (id Int64) ENGINE = MergeTree ORDER BY id")
        await project.query(f"ALTER TABLE {database}.visit DROP COLUMN site")

        result = await project.cli("drift", "app")

        assert result.exit_code not in (0, None)
        assert "Add field site to Visit" in result.output
        assert await project.query(f"SELECT name FROM system.tables WHERE database = '{database}' ORDER BY name") == [
            {"name": "visit"}
        ]
        assert (
            await project.query(f"SELECT name FROM system.databases WHERE name LIKE 'hare_shadow_%_{database}'") == []
        )
    finally:
        await project.query(f"DROP DATABASE IF EXISTS {database}")


@pytest.mark.asyncio
async def test_inspectdb_reads_another_database_as_models_of_it(project_factory):  # noqa: F811
    project = await project_factory()
    database = f"hare_moved_{uuid.uuid4().hex[:8]}"
    await project.query(f"CREATE DATABASE {database}")
    try:
        project.write_models(get_models(database))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")

        inspected = await project.cli_ok("inspectdb", "--schema", database)

        assert f"schema = '{database}'" in inspected
        assert "schema = " not in await project.cli_ok("inspectdb")
    finally:
        await project.query(f"DROP DATABASE IF EXISTS {database}")


DICTIONARY_MODELS = """
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary

class Country(Model):
    code = fields.CharField(max_length=2, primary_key=True)
    title = fields.CharField(max_length=50)
    class Meta:
        schema = {database!r}
        dictionaries = [ClickhouseDictionary("country_titles", key=("code",), attributes=("title",), lifetime=300)]
"""


@pytest.mark.asyncio
async def test_a_dictionary_of_a_model_of_another_database_reads_its_table_there(project_factory):  # noqa: F811
    project = await project_factory()
    database = f"hare_moved_{uuid.uuid4().hex[:8]}"
    await project.query(f"CREATE DATABASE {database}")
    try:
        project.write_models(DICTIONARY_MODELS.format(database=database))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.query(f"INSERT INTO {database}.country (code, title) VALUES ('fr', 'France')")

        assert await project.query(f"SELECT dictGet('{database}.country_titles', 'title', 'fr') AS title") == [
            {"title": "France"}
        ]
    finally:
        await project.query(f"DROP DATABASE IF EXISTS {database}")
