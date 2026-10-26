"""SQLite keeps a table's and a column's comment inside CREATE TABLE: drift reads them back from there - a
comment the table doesn't hold is reported, one it holds is no drift."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

MODELS = '''
class Note(Model):
    """{table_description}"""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=20, description={column_description!r})
'''


def get_models(table_description: str, column_description: str) -> str:
    return textwrap.dedent(MODELS).format(table_description=table_description, column_description=column_description)


@pytest.fixture(autouse=True)
def skip_without_sqlite():
    if os.getenv("HARE_TEST_DB", "").split(":", 1)[0] not in {"", "sqlite"}:
        pytest.skip("SQLite keeps the comments inside CREATE TABLE")


@pytest.mark.asyncio
async def test_a_comment_the_table_does_not_hold_is_drift(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(get_models("Notes, (all) of them", "the */ title\nsecond line"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    project.write_models(get_models("Other notes", "the */ title\nsecond line"))
    table_drift = await project.cli("drift", "app")
    assert table_drift.exit_code not in (0, None), table_drift.output

    project.write_models(get_models("Notes, (all) of them", "another title"))
    column_drift = await project.cli("drift", "app")
    assert column_drift.exit_code not in (0, None), column_drift.output
    assert "title" in column_drift.output
