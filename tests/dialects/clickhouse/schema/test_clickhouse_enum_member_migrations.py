"""An enum field's column on ClickHouse is an Enum8/Enum16 of its members: a member added to the enum or
removed from it is a change of the column's type the migration makes, not of its comment alone."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

MODELS = """
from enum import IntEnum, StrEnum


class Color(StrEnum):
{color_members}


class Size(IntEnum):
{size_members}


class Paint(Model):
    id = fields.IntField(primary_key=True, generated=False)
    color = fields.CharEnumField(Color)
    size = fields.IntEnumField(Size)
"""


def get_models(color_members: str, size_members: str) -> str:
    return textwrap.dedent(MODELS).format(color_members=color_members, size_members=size_members)


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
async def test_a_member_added_and_removed_changes_the_column_type(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(get_models("    RED = 'red'\n    BLUE = 'blue'", "    S = 1\n    M = 2"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO paint (id, color, size) VALUES (1, 'red', 'S')")

    project.write_models(
        get_models("    RED = 'red'\n    BLUE = 'blue'\n    GREEN = 'green'", "    S = 1\n    M = 2\n    L = 3")
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO paint (id, color, size) VALUES (2, 'green', 'L')")
    assert await project.query(
        "SELECT id, toString(color) AS color, toString(size) AS size FROM paint ORDER BY id"
    ) == [{"id": 1, "color": "red", "size": "S"}, {"id": 2, "color": "green", "size": "L"}]

    await project.query("ALTER TABLE paint DELETE WHERE id = 2 SETTINGS mutations_sync = 2")
    project.write_models(get_models("    RED = 'red'", "    S = 1"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    column_types = await project.query(
        "SELECT name, type FROM system.columns WHERE database = currentDatabase() AND table = 'paint' "
        "AND name IN ('color', 'size') ORDER BY name"
    )
    assert [(row["name"], "'blue'" in row["type"] or "'M'" in row["type"]) for row in column_types] == [
        ("color", False),
        ("size", False),
    ]

    # Rolled back to the first migration - its members again.
    await project.cli_ok("migrate", "app", "0001_initial")
    column_types = await project.query(
        "SELECT name, type FROM system.columns WHERE database = currentDatabase() AND table = 'paint' "
        "AND name IN ('color', 'size') ORDER BY name"
    )
    assert [
        ("'blue'" in row["type"] or "'M'" in row["type"], "'green'" in row["type"] or "'L'" in row["type"])
        for row in column_types
    ] == [(True, False), (True, False)]
