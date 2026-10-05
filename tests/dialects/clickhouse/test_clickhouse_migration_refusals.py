"""A ClickHouse migration runs outside a transaction - an operation refused for its table options was
refused only when it ran, after the operations before it had changed the schema."""

import os
import textwrap

import pytest

from tests.cli.test_cli_migration_round_trips import project_factory  # noqa: F401

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
