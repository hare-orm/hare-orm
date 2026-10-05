"""A ClickHouse text column changed to another type: the server changes the column's type first and
converts the stored texts in a mutation after it - a text that didn't parse failed the mutation and left
the table unreadable, became NULL in a Nullable column, and an impossible date was read as another one.
Such a text is refused before the column changes."""

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
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


async def get_value_column_type(project) -> str:
    rows = await project.query(
        "SELECT type FROM system.columns WHERE database = currentDatabase() AND table = 'note' AND name = 'value'"
    )
    return rows[0]["type"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("IntField()", "abc"),
        ("IntField()", "1.5"),
        ("IntField()", "99999999999"),
        ("IntField(null=True)", "abc"),
        ("FloatField(null=True)", "12abc"),
        ("DecimalField(max_digits=10, decimal_places=2)", "1.555"),
        ("DecimalField(max_digits=4, decimal_places=2)", "123"),
        ("BooleanField()", "maybe"),
        ("UUIDField(null=True)", "abc"),
        ("DateField()", "2024-13-01"),
        ("DateField(null=True)", "2024-02-30"),
        ("DatetimeField()", "2024-01-01 25:00:00"),
        ("DatetimeField(null=True)", "abc"),
        ("TimeField(null=True)", "x"),
        ("TimeField(null=True)", "25:00:00"),
        ("TimeField()", ""),
    ],
)
async def test_a_text_the_new_type_does_not_hold_is_refused_before_the_column_changes(
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
    assert await get_value_column_type(project) == "Nullable(String)"
    assert await project.query("SELECT id, value FROM note ORDER BY id") == [
        {"id": 1, "value": text},
        {"id": 2, "value": None},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "column_type", "text", "value_text"),
    [
        ("IntField(null=True)", "Nullable(Int32)", "-12", "-12"),
        ("FloatField(null=True)", "Nullable(Float64)", "1e3", "1000"),
        ("BooleanField(null=True)", "Nullable(Bool)", "true", "true"),
        ("DateField(null=True)", "Nullable(Date32)", "2024-02-29", "2024-02-29"),
    ],
)
async def test_a_text_the_new_type_holds_is_converted(
    project_factory,  # noqa: F811
    field: str,
    column_type: str,
    text: str,
    value_text: str,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field="CharField(max_length=40, null=True)")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO note (id, value) VALUES (1, '{text}'), (2, NULL)")
    project.write_models(textwrap.dedent(TEMPLATE.format(field=field)))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    assert await get_value_column_type(project) == column_type
    rows = await project.query("SELECT id, toString(value) AS value FROM note ORDER BY id")
    assert [row["value"] for row in rows] == [value_text, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_field", "value_sql", "new_field", "column_type"),
    [
        ("IntField(null=True)", "5", "UUIDField(null=True)", "Nullable(Int32)"),
        ("FloatField(null=True)", "1.5", "UUIDField(null=True)", "Nullable(Float64)"),
        ("UUIDField(null=True)", "'00000000-0000-0000-0000-000000000001'", "IntField(null=True)", "Nullable(UUID)"),
        (
            "DatetimeField(null=True)",
            "'2020-01-02 03:04:05'",
            "SmallIntField(null=True)",
            "Nullable(DateTime64(6, 'UTC'))",
        ),
    ],
)
async def test_a_value_of_another_type_the_new_type_does_not_hold_is_refused_before_the_column_changes(
    project_factory,  # noqa: F811
    old_field: str,
    value_sql: str,
    new_field: str,
    column_type: str,
) -> None:
    """Only a text column was checked - the column of another type changed, its values failed the
    conversion mutation, and every later read of the table failed."""
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(field=old_field)))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO note (id, value) VALUES (1, {value_sql}), (2, NULL)")
    project.write_models(textwrap.dedent(TEMPLATE.format(field=new_field)))
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert await get_value_column_type(project) == column_type
    assert len(await project.query("SELECT id, value FROM note ORDER BY id")) == 2
