"""A ClickHouse Decimal column given fewer decimal places: the narrowing check compared the column with a
whole-digits bound past its own digits, which the server refused (DECIMAL_OVERFLOW) - every such migration
failed, whatever the rows held."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

TEMPLATE = """
class Note(Model):
    id = fields.IntField(primary_key=True, generated=False)
    value = fields.DecimalField(max_digits=8, decimal_places={places})
"""


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "applies"), [("1.5000", True), ("1.5500", False)])
async def test_fewer_decimal_places_are_checked_against_the_rows(
    project_factory,  # noqa: F811
    stored: str,
    applies: bool,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(TEMPLATE.format(places=4)))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO note (id, value) VALUES (1, {stored})")
    project.write_models(textwrap.dedent(TEMPLATE.format(places=1)))
    await project.cli_ok("makemigrations")

    result = await project.cli("migrate")

    if applies:
        assert result.exit_code in (0, None), result.output
        assert await project.query("SELECT toString(value) AS value FROM note") == [{"value": "1.5"}]
    else:
        assert result.exit_code not in (0, None)
        assert "1 existing row(s) would not fit" in result.output
