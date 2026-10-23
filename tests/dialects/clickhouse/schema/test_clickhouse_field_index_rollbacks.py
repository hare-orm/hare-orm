"""A field added with an index of its own on ClickHouse: the AddField creates the index with the column,
and rolling it back drops the index once, before the column - ClickHouse drops no column an index covers."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

BASE = """
class Note(Model):
    id = fields.IntField(primary_key=True, generated=False)
"""


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "added_field",
    [
        "tag = fields.CharField(max_length=10, null=True, db_index=True)",
        "parent = fields.ForeignKeyField('app.Note', related_name='children', null=True, db_constraint=False)",
    ],
)
async def test_a_field_added_with_its_index_is_rolled_back_and_removed(
    project_factory,  # noqa: F811
    added_field: str,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(BASE))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(textwrap.dedent(BASE) + f"    {added_field}\n")
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    await project.cli_ok("migrate", "app", "0001_initial")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    project.write_models(textwrap.dedent(BASE))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    await project.assert_in_sync()
    indexes = await project.query(
        "SELECT name FROM system.data_skipping_indices WHERE database = currentDatabase() AND table = 'note'"
    )
    assert indexes == []
