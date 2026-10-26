"""A many-to-many relation on ClickHouse, which has no foreign keys: renaming the model it points at renames
the column of its through table and makes no foreign key again - the migration was refused halfway."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_renaming_the_model_a_relation_points_at_keeps_its_links(project_factory):  # noqa: F811
    project = await project_factory()
    template = """
    class {target}(Model):
        id = fields.IntField(primary_key=True, generated=False)

    class Note(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tags = fields.ManyToManyField("app.{target}", related_name="notes", db_constraint=False)
    """
    project.write_models(textwrap.dedent(template.format(target="Tag")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO note_tag (note_id, tag_id) VALUES (1, 2)")
    project.write_models(textwrap.dedent(template.format(target="Label")))
    await project.cli_ok("makemigrations", "--rename", "app.Tag=Label")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT note_id, label_id FROM note_label") == [{"note_id": 1, "label_id": 2}]
