"""A VersionedCollapsingMergeTree model on ClickHouse: the server appends the version column to the
table's sorting key, and drift and makemigrations saw options no model declared after every migrate."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize("order_by", ["", 'order_by=("id", "version"), '])
async def test_a_versioned_collapsing_table_is_in_sync_after_its_migration(
    project_factory,  # noqa: F811
    order_by: str,
) -> None:
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            f"""
            from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
            from hare.dialects.clickhouse.fields import Int8Field

            class Balance(Model):
                id = fields.IntField(primary_key=True, generated=False)
                sign = Int8Field()
                version = fields.IntField()

                class Meta:
                    table_options = [
                        ClickhouseTableOptions({order_by}engine="VersionedCollapsingMergeTree(sign, version)")
                    ]
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
