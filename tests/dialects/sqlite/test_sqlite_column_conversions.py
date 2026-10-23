"""A SQLite column changed to a field of another type: the table is rebuilt with every value copied as it
is - a text the new field doesn't read (no UUID, no JSON, no duration) stayed in the new column, and the
rows read back failed. Such a value is refused before the table changes."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

TEMPLATE = """
class Note(Model):
    id = fields.IntField(primary_key=True, generated=False)
    value = fields.{field}
"""


@pytest.fixture(autouse=True)
def skip_without_sqlite():
    if os.getenv("HARE_TEST_DB", "sqlite").split(":")[0].split("+")[0] != "sqlite":
        pytest.skip("rebuilds a SQLite table")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("UUIDField(null=True)", "abc"),
        ("JSONField(null=True)", "not json"),
        ("TimeDeltaField(null=True)", "abc"),
        ("BinaryField(null=True)", "abc"),
    ],
)
async def test_a_text_the_new_field_does_not_read_is_refused_before_the_table_changes(
    project_factory,  # noqa: F811
    field: str,
    text: str,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field="CharField(max_length=40, null=True)")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO note (id, value) VALUES (1, '{text}'), (2, NULL)")
    project.write_models(textwrap.dedent(TEMPLATE.format(field=field)))
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert f'holds "{text}"' in result.output
    assert await project.query("SELECT id, value FROM note ORDER BY id") == [
        {"id": 1, "value": text},
        {"id": 2, "value": None},
    ]


@pytest.mark.asyncio
async def test_a_text_the_new_field_reads_is_converted(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field="CharField(max_length=40, null=True)")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO note (id, value) VALUES (1, '00000000-0000-0000-0000-000000000001'), (2, NULL)")
    project.write_models(textwrap.dedent(TEMPLATE.format(field="UUIDField(null=True)")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_field", "stored_sql", "stored", "new_field"),
    [
        (
            "UUIDField(null=True)",
            "'12345678-1234-5678-1234-567812345678'",
            "12345678-1234-5678-1234-567812345678",
            "IntField(null=True)",
        ),
        ("JSONField(null=True)", "'{\"a\":1}'", '{"a":1}', "IntField(null=True)"),
        ("JSONField(null=True)", "'{\"a\":1}'", '{"a":1}', "BooleanField(null=True)"),
        (
            "DatetimeField(null=True)",
            "'2020-05-06 23:30:15+00:00'",
            "2020-05-06 23:30:15+00:00",
            "TimeField(null=True)",
        ),
        ("TimeField(null=True)", "'23:30:15'", "23:30:15", "DateField(null=True)"),
        ("TimeField(null=True)", "'23:30:15'", "23:30:15", "DatetimeField(null=True)"),
        ("BooleanField(null=True)", "1", 1, "DateField(null=True)"),
    ],
)
async def test_a_value_of_another_type_the_new_field_does_not_read_is_refused(
    project_factory,  # noqa: F811
    old_field: str,
    stored_sql: str,
    stored: object,
    new_field: str,
) -> None:
    """The value was copied into the rebuilt table as stored - a UUID's text in an integer column, a moment's
    text in a time one - and the rows read back failed."""
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field=old_field)))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO note (id, value) VALUES (1, {stored_sql})")
    project.write_models(textwrap.dedent(TEMPLATE.format(field=new_field)))
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "which is no" in result.output
    assert await project.query("SELECT value FROM note") == [{"value": stored}]
