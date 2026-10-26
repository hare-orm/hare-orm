"""A ClickHouse migration runs outside a transaction - an operation refused for its table options,
an index key's direction or a constraint was refused only when it ran, after the operations before it had
changed the schema."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

REFUSED_MIGRATION = """
from hare import fields, migrations
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.migrations import operations as ops

class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = [
        ops.AddField(model_name="Board", name="note", field=fields.IntField(default=0)),
        ops.AlterModelOptions(name="Board", options={"table_options": [ClickhouseTableOptions(order_by=("rank",))]}),
    ]
"""


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_a_migration_refused_for_table_options_changes_nothing(project_factory):  # noqa: F811
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)
                rank = fields.IntField(default=0)
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_migration("0002_refused", textwrap.dedent(REFUSED_MIGRATION))

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "order_by must begin with the primary key" in result.output
    assert "failed partway" not in result.output
    columns = await project.query(
        "SELECT name FROM system.columns WHERE database = currentDatabase() AND table = 'board'"
    )
    assert {column["name"] for column in columns} == {"id", "rank"}


@pytest.mark.asyncio
async def test_a_migration_refused_for_an_index_key_direction_creates_no_table(project_factory):  # noqa: F811
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            from hare.ddl import Index

            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)

            class Pin(Model):
                id = fields.IntField(primary_key=True, generated=False)
                rank = fields.IntField(default=0)

                class Meta:
                    indexes = [Index(fields=["-rank"], name="pin_rank_desc")]
            """
        )
    )
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "index key's direction" in result.output
    assert "failed partway" not in result.output
    tables = await project.query("SELECT name FROM system.tables WHERE database = currentDatabase()")
    assert not {table["name"] for table in tables} & {"board", "pin"}


@pytest.mark.asyncio
async def test_an_added_index_refused_for_its_key_direction_changes_nothing(project_factory):  # noqa: F811
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)
                rank = fields.IntField(default=0)
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_migration(
        "0002_refused",
        textwrap.dedent(
            """
            from hare import fields, migrations
            from hare.ddl import Index
            from hare.migrations import operations as ops

            class Migration(migrations.Migration):
                dependencies = [("app", "0001_initial")]
                operations = [
                    ops.AddField(model_name="Board", name="note", field=fields.IntField(default=0)),
                    ops.AddIndex(model_name="Board", index=Index(fields=["-rank"], name="board_rank_desc")),
                ]
            """
        ),
    )

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "index key's direction" in result.output
    assert "failed partway" not in result.output
    columns = await project.query(
        "SELECT name FROM system.columns WHERE database = currentDatabase() AND table = 'board'"
    )
    assert {column["name"] for column in columns} == {"id", "rank"}


UNIQUE_OPERATIONS = {
    "a created model": (
        'ops.CreateModel(name="Tag", fields=[("id", fields.IntField(primary_key=True, generated=False)), '
        '("code", fields.CharField(max_length=20, unique=True))])'
    ),
    "a created model's constraint": (
        'ops.CreateModel(name="Tag", fields=[("id", fields.IntField(primary_key=True, generated=False)), '
        '("code", fields.CharField(max_length=20))], '
        'options={"constraints": [UniqueConstraint(fields=["code"], name="tag_code_unique")]})'
    ),
    "an added field": 'ops.AddField(model_name="Board", name="code", field=fields.IntField(default=0, unique=True))',
    "an altered field": (
        'ops.AlterField(model_name="Board", name="rank", field=fields.IntField(default=0, unique=True))'
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(UNIQUE_OPERATIONS))
async def test_a_migration_declaring_uniqueness_is_refused_before_it_changes_anything(
    project_factory,  # noqa: F811
    operation,
):
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)
                rank = fields.IntField(default=0)
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_migration(
        "0002_unique",
        textwrap.dedent(
            f"""
            from hare import fields, migrations
            from hare.ddl import UniqueConstraint
            from hare.migrations import operations as ops

            class Migration(migrations.Migration):
                dependencies = [("app", "0001_initial")]
                operations = [
                    ops.AddField(model_name="Board", name="note", field=fields.IntField(default=0)),
                    {UNIQUE_OPERATIONS[operation]},
                ]
            """
        ),
    )

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "keeps nothing unique" in result.output
    assert "failed partway" not in result.output
    tables = await project.query("SELECT name FROM system.tables WHERE database = currentDatabase()")
    assert "tag" not in {table["name"] for table in tables}
    columns = await project.query(
        "SELECT name FROM system.columns WHERE database = currentDatabase() AND table = 'board'"
    )
    assert {column["name"] for column in columns} == {"id", "rank"}


@pytest.mark.asyncio
async def test_a_migration_enabling_row_level_security_creates_no_table(project_factory):  # noqa: F811
    """The row level security of a new model was refused when its own operation ran - after the model's
    table was created, the migration left applied halfway."""
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)

                class Meta:
                    row_level_security = "enabled"
            """
        )
    )
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "Row level security policies are not supported on clickhouse" in result.output
    assert "failed partway" not in result.output
    tables = await project.query("SELECT name FROM system.tables WHERE database = currentDatabase()")
    assert "board" not in {table["name"] for table in tables}


@pytest.mark.asyncio
async def test_a_migration_creating_a_model_with_a_trigger_creates_no_table(project_factory):  # noqa: F811
    """A new model's trigger was refused once the model's table was created - the migration left applied
    halfway."""
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            from hare.ddl import RawSQLTerm
            from hare.ddl.schema_objects.trigger import Trigger

            class Board(Model):
                id = fields.IntField(primary_key=True, generated=False)

                class Meta:
                    triggers = [Trigger(name="board_insert", on="INSERT", body=RawSQLTerm("RETURN NEW;"))]
            """
        )
    )
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "Triggers are not supported on clickhouse" in result.output
    assert "failed partway" not in result.output
    tables = await project.query("SELECT name FROM system.tables WHERE database = currentDatabase()")
    assert "board" not in {table["name"] for table in tables}
