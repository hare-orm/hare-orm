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
from hare import fields, migrations
from hare.ddl import RawSQLTerm, Trigger
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    atomic = {atomic}
    dependencies = [("app", "0001_initial")]
    operations = [
        {first_operation},
        {last_operation},
    ]
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_operation", "last_operation", "partly_applied"),
    [
        ("backfill", 'ops.BackfillColumn("Loan", "nope", "x")', False),
        ("backfill", 'ops.RunSQL("SELECT * FROM no_such_table")', True),
        # Refused by a check reading the rows - no statement of its own ran.
        ("backfill", 'ops.AlterColumnNotNullSafe("Loan", "note")', True),
        pytest.param("trigger", 'ops.AlterColumnNotNullSafe("Loan", "note")', True, marks=pytest.mark.sqlite_only),
    ],
)
async def test_a_failure_after_a_schema_write_is_reported_as_partly_applied(
    project_factory,  # noqa: F811
    first_operation: str,
    last_operation: str,
    partly_applied: bool,
) -> None:
    project = await project_factory()
    project.write_models(textwrap.dedent(MODELS))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO loan (id) VALUES (1), (2), (3)")
    first_sql = (
        'ops.BackfillColumn("Loan", "status", "pending", batch_size=2)'
        if first_operation == "backfill"
        else 'ops.AddTrigger("Loan", Trigger(name="loan_insert", on="INSERT", body=RawSQLTerm('
        "\"UPDATE loan SET status='created' WHERE id=NEW.id; \""
        "\"UPDATE loan SET note='fired' WHERE id=NEW.id;\")))"
    )
    project.write_migration(
        "0002_backfill",
        textwrap.dedent(MIGRATION).format(atomic=False, first_operation=first_sql, last_operation=last_operation),
    )

    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert (PartiallyAppliedMigrationError.__name__ in result.output or "partway" in result.output) is partly_applied
    statuses = [row["status"] for row in await project.query("SELECT status FROM loan ORDER BY id")]
    assert statuses == (["pending"] * 3 if partly_applied and first_operation == "backfill" else [None] * 3)
    if first_operation == "trigger":
        await project.query("INSERT INTO loan (id) VALUES (4)")
        assert await project.query("SELECT status, note FROM loan WHERE id = 4") == [
            {"status": "created", "note": "fired"}
        ]


@pytest.mark.sqlite_only
@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [True, False], ids=["atomic", "non-atomic"])
async def test_backfill_failure_preserves_the_schema_and_history_at_the_transaction_boundary(
    project_factory,  # noqa: F811
    atomic,
):
    project = await project_factory()
    project.write_models(textwrap.dedent(MODELS))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO loan (id) VALUES (1), (2), (3)")
    # The first two updates succeed, regardless of which IDs the first batch selects.
    await project.query(
        "CREATE TRIGGER loan_fail_second_batch BEFORE UPDATE OF status ON loan "
        "WHEN NEW.status = 'pending' AND (SELECT COUNT(*) FROM loan WHERE status = 'pending') >= 2 "
        "BEGIN SELECT RAISE(ABORT, 'second backfill batch failed'); END"
    )
    before = await project.query("SELECT app, name FROM hare_migrations ORDER BY app, name")
    project.write_migration(
        "0002_backfill",
        MIGRATION.format(
            atomic=atomic,
            first_operation='ops.AddField("Loan", "marker", fields.IntField(null=True))',
            last_operation='ops.BackfillColumn("Loan", "status", "pending", batch_size=2)',
        ),
    )
    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "second backfill batch failed" in result.output
    assert (PartiallyAppliedMigrationError.__name__ in result.output or "partway" in result.output) is not atomic
    assert await project.query("SELECT app, name FROM hare_migrations ORDER BY app, name") == before
    assert [row["name"] for row in await project.query("PRAGMA table_info(loan)")] == (
        ["id", "status", "note"] if atomic else ["id", "status", "note", "marker"]
    )
    rows = await project.query("SELECT id, status, note FROM loan ORDER BY id")
    assert [(row["id"], row["note"]) for row in rows] == [(1, None), (2, None), (3, None)]
    assert all(row["status"] in (None, "pending") for row in rows)
    assert sum(row["status"] == "pending" for row in rows) == (0 if atomic else 2)

    await project.query("DROP TRIGGER loan_fail_second_batch")
    if not atomic:
        # Reconcile the applied schema step before retrying the unrecorded migration.
        await project.query("ALTER TABLE loan DROP COLUMN marker")
    await project.cli_ok("migrate")
    assert await project.query("SELECT app, name FROM hare_migrations ORDER BY app, name") == [
        *before,
        {"app": "app", "name": "0002_backfill"},
    ]
    assert await project.query("SELECT id, status, note, marker FROM loan ORDER BY id") == [
        {"id": key, "status": "pending", "note": None, "marker": None} for key in (1, 2, 3)
    ]
