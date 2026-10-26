"""A key of a ClickHouse table moved from a removed field to an added one: the migration makemigrations writes
adds the field, sets the new key and removes the old field after it - it set the key before the field it
names existed."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

MODELS = """
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions


class Hit(Model):
    id = fields.IntField(primary_key=True, generated=False)
    {field} = fields.IntField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                engine={engine!r}, order_by=("id", {field!r}), partition_by={field!r}, sample_by=None
            )
        ]
"""


def get_models(field: str, engine: str = "MergeTree") -> str:
    return textwrap.dedent(MODELS).format(field=field, engine=engine)


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_a_key_moved_to_an_added_field_is_set_after_it(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(get_models("x"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO hit (id, x) VALUES (1, 4), (2, 5)")

    project.write_models(get_models("y", "ReplacingMergeTree(y)"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    tables = await project.query(
        "SELECT sorting_key, partition_key, engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'hit'"
    )
    assert tables == [{"sorting_key": "id, y", "partition_key": "y", "engine": "ReplacingMergeTree"}]
    assert await project.query("SELECT count() AS rows FROM hit") == [{"rows": 2}]

    await project.cli_ok("migrate", "app", "0001_initial")
    tables = await project.query(
        "SELECT sorting_key, partition_key, engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'hit'"
    )
    assert tables == [{"sorting_key": "id, x", "partition_key": "x", "engine": "MergeTree"}]
