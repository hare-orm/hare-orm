"""Texts the server writes back escaped and comments changed alone on ClickHouse: a database default
holding a quote, a backslash or a line break was read back with the server's escapes and drift reported
it changed after every migrate, and an AlterField changing only a description or a table's
description changed nothing."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

TEMPLATE = """
class Note(Model):
    id = fields.IntField(primary_key=True, generated=False)
    value = fields.CharField(max_length=40, {field_options})

    class Meta:
        table_description = {table_description!r}
"""


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_options",
    ['db_default="it\'s"', "db_default='back\\\\slash'", "db_default='two\\nlines\\ttab'"],
    ids=["quote", "backslash", "line_break"],
)
async def test_a_default_text_with_escapes_is_in_sync_after_its_migration(
    project_factory,  # noqa: F811
    field_options: str,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field_options=field_options, table_description="t")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_changed_description_changes_the_comment(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field_options="description='first'", table_description="t")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    for field_options, table_description in (
        ('description="it\'s changed"', "t"),
        ("description='last'", "the table's"),
        ("default=''", ""),
    ):
        project.write_models(
            textwrap.dedent(TEMPLATE.format(field_options=field_options, table_description=table_description))
        )
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    rows = await project.query(
        "SELECT comment FROM system.columns WHERE database = currentDatabase() AND table = 'note' AND name = 'value'"
    )
    assert rows == [{"comment": ""}]
