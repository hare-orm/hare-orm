"""A many-to-many relation switched from its automatic through table to a through model on ClickHouse, which
writes no key of its own: each link carried over gets the next key of the through model's series."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_links_carried_into_a_through_model_get_keys_of_its_series(project_factory):  # noqa: F811
    """Every link carried over got the key 0 - the column's default."""
    project = await project_factory()
    models = """
    class Tag(Model):
        id = fields.IntField(primary_key=True, generated=False)

    class Note(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tags = fields.ManyToManyField("app.Tag", related_name="notes", db_constraint=False{through})
    {through_model}"""
    through_model = """
    class NoteTag(Model):
        id = fields.IntField(primary_key=True)
        note = fields.ForeignKeyField("app.Note", related_name="links", db_constraint=False)
        tag = fields.ForeignKeyField("app.Tag", related_name="links", db_constraint=False)
    """
    project.write_models(textwrap.dedent(models.format(through="", through_model="")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO note_tag (note_id, tag_id) VALUES (1, 1), (1, 2), (2, 2)")
    project.write_models(
        textwrap.dedent(models.format(through=', through="app.NoteTag"', through_model=through_model))
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    rows = await project.query("SELECT id, note_id, tag_id FROM notetag ORDER BY note_id, tag_id")
    assert [(row["note_id"], row["tag_id"]) for row in rows] == [(1, 1), (1, 2), (2, 2)]
    assert len({row["id"] for row in rows}) == 3
    assert 0 not in {row["id"] for row in rows}
