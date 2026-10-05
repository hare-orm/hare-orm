"""`hare makemigrations` warns about risky operations of the migrations it writes, and `hare
checkmigrations` checks the migrations not applied yet against the database - failing for a risk
the migration doesn't exempt."""

from __future__ import annotations

import pytest

from tests.cli.test_cli_migration_round_trips import project_factory  # noqa: F401 - the fixture

MODELS_WITH_PAGES = """
class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    pages = fields.IntField(null=True)
"""

MODELS_WITHOUT_PAGES = """
class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
"""


@pytest.mark.asyncio
async def test_makemigrations_warns_and_checkmigrations_fails_until_applied(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(MODELS_WITH_PAGES)
    initial_output = await project.cli_ok("makemigrations")
    assert "risky on a database in use" not in initial_output
    clean_check = await project.cli("checkmigrations")
    assert clean_check.exit_code in (0, None), clean_check.output
    assert "No risky operations" in clean_check.output
    await project.cli_ok("migrate")

    project.write_models(MODELS_WITHOUT_PAGES)
    removal_output = await project.cli_ok("makemigrations")
    assert "risky on a database in use" in removal_output
    assert "[remove_field]" in removal_output
    assert "SeparateDatabaseAndState" in removal_output

    failing_check = await project.cli("checkmigrations", "app")
    assert failing_check.exit_code == 1, failing_check.output
    assert "[remove_field]" in failing_check.output
    assert "Safely:" in failing_check.output

    await project.cli_ok("migrate")
    applied_check = await project.cli("checkmigrations")
    assert applied_check.exit_code in (0, None), applied_check.output
    assert "No risky operations" in applied_check.output


@pytest.mark.asyncio
async def test_an_exempted_risk_is_listed_without_failing(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    project.write_models(MODELS_WITH_PAGES)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    initial_name = project.migration_files()[0]
    project.write_migration(
        "0002_raw_sql",
        f"""
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.migrations.safety import MigrationRiskCode

        class Migration(migrations.Migration):
            dependencies = [("app", {initial_name!r})]
            safety_exemptions = [MigrationRiskCode.RUN_SQL]
            operations = [ops.RunSQL("SELECT 1")]
        """,
    )
    result = await project.cli("checkmigrations")
    assert result.exit_code in (0, None), result.output
    assert "[run_sql] (exempted)" in result.output.replace("\x1b[2m", "").replace("\x1b[0m", "")

    project.write_migration(
        "0002_raw_sql",
        f"""
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", {initial_name!r})]
            safety_exemptions = ["not_a_code"]
            operations = [ops.RunSQL("SELECT 1")]
        """,
    )
    refused = await project.cli("checkmigrations")
    assert refused.exit_code == 1
    assert "safety_exemptions takes MigrationRiskCode members" in refused.output


@pytest.mark.asyncio
async def test_checkmigrations_refuses_an_unknown_app(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    result = await project.cli("checkmigrations", "nope")
    assert result.exit_code == 2
    assert "Unknown app label nope" in result.output
