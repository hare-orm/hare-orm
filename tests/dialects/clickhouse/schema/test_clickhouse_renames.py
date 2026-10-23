"""Renames on ClickHouse. A field of a table's key - its primary key, sorting or partition key - can't be
renamed in place: the table is remade with the column under its new name, its rows copied, and the key
expressions read it by its new name; the columns other tables name after it are renamed too. A renamed
model's table takes its indexes along, each named after the new table once."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


async def make_rename_migration(project, renamed, new_name):
    """Runs makemigrations answering that ``app.<renamed>`` is renamed to ``new_name``."""
    await project.cli_ok("makemigrations", "--rename", f"app.{renamed}={new_name}")


@pytest.mark.asyncio
async def test_a_field_of_the_sorting_key_is_renamed_with_its_rows(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions

    class Visit(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        {site} = fields.CharField(max_length=50)
        {at} = fields.DatetimeField()
        class Meta:
            table_options = [
                ClickhouseTableOptions(order_by=("id", "{site}"), partition_by=RawSQLTerm("toYYYYMM({at})"))
            ]
    """
    names = {"site": "site", "at": "visited_at"}
    project.write_models(textwrap.dedent(template.format(**names)))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO visit (id, site, visited_at) VALUES (1, 'a', '2026-05-01 10:00:00')")
    names["site"] = "domain"
    project.write_models(textwrap.dedent(template.format(**names)))
    await make_rename_migration(project, "Visit.site", "domain")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, domain, toString(visited_at) AS visited_at FROM visit") == [
        {"id": 1, "domain": "a", "visited_at": "2026-05-01 10:00:00.000000"}
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, site FROM visit") == [{"id": 1, "site": "a"}]


@pytest.mark.asyncio
async def test_a_component_of_a_composite_key_is_renamed_in_the_relations_to_it(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        {component} = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "{component}")

    class Note(Model):
        id = fields.IntField(primary_key=True, generated=False)
        pair = fields.ForeignKeyField("app.Pair", related_name="notes", null=True, db_constraint=False)
    """
    project.write_models(textwrap.dedent(template.format(component="b")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO pair (a, b) VALUES (1, 2)")
    await project.query("INSERT INTO note (id, pair_a, pair_b) VALUES (1, 1, 2)")
    project.write_models(textwrap.dedent(template.format(component="c")))
    await make_rename_migration(project, "Pair.b", "c")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT a, c FROM pair") == [{"a": 1, "c": 2}]
    assert await project.query("SELECT pair_a, pair_c FROM note") == [{"pair_a": 1, "pair_c": 2}]


@pytest.mark.asyncio
async def test_a_renamed_model_renames_the_index_of_its_field(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class {name}(Model):
        id = fields.IntField(primary_key=True, generated=False)
        rank = fields.IntField(default=0, db_index=True)
    """
    project.write_models(textwrap.dedent(template.format(name="Board")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO board (id, rank) VALUES (1, 5)")
    project.write_models(textwrap.dedent(template.format(name="Panel")))
    await make_rename_migration(project, "Board", "Panel")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, rank FROM panel") == [{"id": 1, "rank": 5}]


@pytest.mark.asyncio
async def test_a_key_component_of_a_model_relating_to_itself_is_renamed_in_one_remake(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class Node(Model):
        a = fields.IntField()
        {component} = fields.IntField()
        parent = fields.ForeignKeyField("app.Node", related_name="children", null=True, db_constraint=False)
        pk = fields.CompositePrimaryKey("a", "{component}")
    """
    project.write_models(textwrap.dedent(template.format(component="b")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO node (a, b, parent_a, parent_b) VALUES (1, 2, 1, 1)")
    project.write_models(textwrap.dedent(template.format(component="c")))
    await make_rename_migration(project, "Node.b", "c")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT a, c, parent_a, parent_c FROM node") == [
        {"a": 1, "c": 2, "parent_a": 1, "parent_c": 1}
    ]


@pytest.mark.asyncio
async def test_a_field_read_by_a_projection_a_view_and_a_dictionary_is_renamed(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary
    from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
    from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection

    class Visit(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        {site} = fields.CharField(max_length=50)
        n = fields.IntField()
        class Meta:
            table_options = [
                ClickhouseTableOptions(projections=(ClickhouseProjection("by_n", RawSQLTerm("SELECT * ORDER BY n")),))
            ]
            dictionaries = [ClickhouseDictionary("visit_sites", key=("id",), attributes=("{site}",))]
            materialized_views = [
                ClickhouseMaterializedView(
                    "visits_by_site",
                    RawSQLTerm("SELECT {site}, count() AS visits FROM visit GROUP BY {site}"),
                    engine="SummingMergeTree",
                    order_by=("{site}",),
                )
            ]
    """
    project.write_models(textwrap.dedent(template.format(site="site")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO visit (id, site, n) VALUES (1, 'a', 5), (2, 'b', 7)")
    project.write_models(textwrap.dedent(template.format(site="domain")))
    await make_rename_migration(project, "Visit.site", "domain")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, domain FROM visit ORDER BY id") == [
        {"id": 1, "domain": "a"},
        {"id": 2, "domain": "b"},
    ]
    assert await project.query("SELECT dictGet('visit_sites', 'domain', toUInt64(2)) AS domain") == [{"domain": "b"}]
    await project.query("INSERT INTO visit (id, domain, n) VALUES (3, 'a', 9)")
    assert await project.query(
        "SELECT domain, sum(visits) AS visits FROM visits_by_site GROUP BY domain ORDER BY domain"
    ) == [
        {"domain": "a", "visits": 2},
        {"domain": "b", "visits": 1},
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, site FROM visit ORDER BY id") == [
        {"id": 1, "site": "a"},
        {"id": 2, "site": "b"},
        {"id": 3, "site": "a"},
    ]


@pytest.mark.asyncio
async def test_a_renamed_field_keeps_the_outputs_of_a_view_writing_into_another_table(project_factory):  # noqa: F811
    """The projection and the views rewritten in the model are removed before the rename and added after
    it - the view writing into another table keeps its outputs, named by the user's ``AS``."""
    project = await project_factory()
    template = """
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
    from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection

    class Archive(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        site = fields.CharField(max_length=50)

    class Visit(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        {site} = fields.CharField(max_length=50)
        class Meta:
            table_options = [
                ClickhouseTableOptions(
                    projections=(
                        ClickhouseProjection("by_site", RawSQLTerm("SELECT {site}, count() GROUP BY {site}")),
                    )
                )
            ]
            materialized_views = [
                ClickhouseMaterializedView(
                    "visit_archive", RawSQLTerm("SELECT id, {selected} FROM visit"), to="archive"
                ),
                ClickhouseMaterializedView(
                    "visit_archive_upper",
                    RawSQLTerm("SELECT id + 100 AS id, upper({site}) AS site FROM visit WHERE {site} != ''"),
                    to="archive",
                ),
            ]
    """
    project.write_models(textwrap.dedent(template.format(site="site", selected="site")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO visit (id, site) VALUES (1, 'a')")
    project.write_models(textwrap.dedent(template.format(site="domain", selected="domain AS site")))
    await make_rename_migration(project, "Visit.site", "domain")

    await project.cli_ok("migrate")

    await project.assert_in_sync()
    await project.query("INSERT INTO visit (id, domain) VALUES (2, 'b')")
    assert await project.query("SELECT id, site FROM archive ORDER BY id") == [
        {"id": 1, "site": "a"},
        {"id": 2, "site": "b"},
        {"id": 101, "site": "A"},
        {"id": 102, "site": "B"},
    ]
    assert await project.query("SELECT domain, count() AS visits FROM visit GROUP BY domain ORDER BY domain") == [
        {"domain": "a", "visits": 1},
        {"domain": "b", "visits": 1},
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, site FROM visit ORDER BY id") == [
        {"id": 1, "site": "a"},
        {"id": 2, "site": "b"},
    ]
