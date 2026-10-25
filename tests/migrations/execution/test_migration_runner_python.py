"""Which migrations run Python code - a failed one that does is reported as partly applied, since its
Python may have written before any statement of the schema editor ran."""

import pytest

from hare.migrations.execution.migration_runner import MigrationRunner
from hare.migrations.migration import Migration
from hare.migrations.operations import RunPython, RunSQL, SeparateDatabaseAndState


def write_nothing(apps, schema_editor):
    return None


@pytest.mark.database_independent
def test_run_python_nested_in_separate_database_and_state_counts_at_any_depth():
    """RunPython inside a SeparateDatabaseAndState nested in another was missed - a migration whose Python
    wrote and then failed before its first statement was reported as not applied at all."""
    nested = SeparateDatabaseAndState(
        database_operations=[SeparateDatabaseAndState(database_operations=[RunPython(write_nothing)])]
    )

    assert MigrationRunner.runs_python(Migration("0001_nested", "app", operations=[nested]))
    assert not MigrationRunner.runs_python(
        Migration("0002_sql", "app", operations=[SeparateDatabaseAndState(database_operations=[RunSQL("SELECT 1")])])
    )
