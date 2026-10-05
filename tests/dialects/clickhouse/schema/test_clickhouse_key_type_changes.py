"""A new type of a ClickHouse table's key column: the server changes no key column in place - the
table is remade with it - and no column a data skipping index reads - the index is dropped for the
change and created again; the relation columns storing its values take the type, a NULL among them
kept."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_a_composite_key_component_takes_a_wider_type_with_the_columns_referencing_it(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        b = fields.{type}()
        pk = fields.CompositePrimaryKey("a", "b")

    class Note(Model):
        id = fields.IntField(primary_key=True, generated=False)
        pair = fields.ForeignKeyField("app.Pair", related_name="notes", null=True, db_constraint=False)
        pairs = fields.ManyToManyField(
            "app.Pair", related_name="listed", through="note_pairs", db_constraint=False
        )
    """
    project.write_models(textwrap.dedent(template.format(type="IntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO pair (a, b) VALUES (1, 2)")
    await project.query("INSERT INTO note (id, pair_a, pair_b) VALUES (1, 1, 2)")
    await project.query("INSERT INTO note (id) VALUES (2)")
    project.write_models(textwrap.dedent(template.format(type="BigIntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    big = 2**40
    await project.query(f"INSERT INTO pair (a, b) VALUES (1, {big})")
    await project.query(f"INSERT INTO note_pairs (note_id, pair_a, pair_b) VALUES (1, 1, {big})")
    assert await project.query("SELECT a, b FROM pair ORDER BY b") == [{"a": 1, "b": 2}, {"a": 1, "b": big}]
    assert await project.query("SELECT id, pair_b FROM note ORDER BY id") == [
        {"id": 1, "pair_b": 2},
        {"id": 2, "pair_b": None},
    ]
    assert await project.query("SELECT pair_b FROM note_pairs") == [{"pair_b": big}]


@pytest.mark.asyncio
async def test_a_remade_table_keeps_the_default_and_the_comment_of_a_column(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        b = fields.{type}()
        pk = fields.CompositePrimaryKey("a", "b")
        count = fields.IntField(db_default=3, description="how many")
    """
    project.write_models(textwrap.dedent(template.format(type="IntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO pair (a, b, count) VALUES (1, 1, 7)")
    project.write_models(textwrap.dedent(template.format(type="BigIntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO pair (a, b) VALUES (2, 2)")
    assert await project.query("SELECT a, count FROM pair ORDER BY a") == [{"a": 1, "count": 7}, {"a": 2, "count": 3}]
    comments = await project.query(
        "SELECT comment FROM system.columns WHERE database = currentDatabase() AND table = 'pair' AND name = 'count'"
    )
    assert comments == [{"comment": "how many"}]
    await project.cli_ok("migrate", "app", "0001_initial")
    await project.query("INSERT INTO pair (a, b) VALUES (3, 3)")
    assert await project.query("SELECT count FROM pair WHERE a = 3") == [{"count": 3}]


@pytest.mark.asyncio
async def test_a_referencing_column_in_the_key_of_its_own_table_remakes_that_table(project_factory):  # noqa: F811
    """A relation to a composite key that is part of the referencing table's own key: the server
    refused MODIFY COLUMN of that key column."""
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        b = fields.{type}()
        pk = fields.CompositePrimaryKey("a", "b")

    class Note(Model):
        pair = fields.ForeignKeyField("app.Pair", related_name="notes", db_constraint=False)
        position = fields.IntField()
        pk = fields.CompositePrimaryKey("pair", "position")
    """
    project.write_models(textwrap.dedent(template.format(type="IntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO pair (a, b) VALUES (1, 2)")
    await project.query("INSERT INTO note (pair_a, pair_b, position) VALUES (1, 2, 3)")
    project.write_models(textwrap.dedent(template.format(type="BigIntField")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    big = 2**40
    await project.query(f"INSERT INTO note (pair_a, pair_b, position) VALUES (1, {big}, 4)")
    assert await project.query("SELECT pair_b, position FROM note ORDER BY position") == [
        {"pair_b": 2, "position": 3},
        {"pair_b": big, "position": 4},
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT pair_b, position FROM note WHERE position = 3") == [
        {"pair_b": 2, "position": 3}
    ]
