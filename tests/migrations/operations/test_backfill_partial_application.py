"""A migration run outside a transaction, BackfillColumn in it: a backfill refused for its field or its model is
refused before any operation runs, and one failing after a backfill wrote its rows is reported as partly
applied - it was reported as a plain error, as if nothing had landed."""

import textwrap

import pytest

from hare.migrations.exceptions import PartiallyAppliedMigrationError
from tests.utils.cli_project_factory import project_factory  # noqa: F401

MODELS = """
class Loan(Model):
    id = fields.IntField(primary_key=True)
    status = fields.CharField(max_length=10, null=True)
    note = fields.CharField(max_length=10, null=True)
"""

MIGRATION = """
from hare import migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("app", "0001_initial")]
    operations = [
        ops.BackfillColumn("Loan", "status", "pending", batch_size=2),
        {last_operation},
    ]
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_operation", "partly_applied"),
    [
        ('ops.BackfillColumn("Loan", "nope", "x")', False),
        ('ops.RunSQL("SELECT * FROM no_such_table")', True),
        # Refused by a check reading the rows - no statement of its own ran.
        ('ops.AlterColumnNotNullSafe("Loan", "note")', True),
    ],
)
async def test_a_failure_after_a_backfill_is_reported_as_partly_applied(
    project_factory,  # noqa: F811
    last_operation: str,
    partly_applied: bool,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(MODELS))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO loan (id) VALUES (1), (2), (3)")
    project.write_migration("0002_backfill", textwrap.dedent(MIGRATION).format(last_operation=last_operation))

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert (PartiallyAppliedMigrationError.__name__ in result.output or "partway" in result.output) is partly_applied
    statuses = [row["status"] for row in await project.query("SELECT status FROM loan ORDER BY id")]
    assert statuses == (["pending"] * 3 if partly_applied else [None] * 3)
