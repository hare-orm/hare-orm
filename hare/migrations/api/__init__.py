"""The migration commands as functions: make, squash, plan, check and apply migrations, show their
SQL."""

from __future__ import annotations

from hare.migrations.api.checkmigrations import checkmigrations
from hare.migrations.api.makemigrations import makemigrations
from hare.migrations.api.migrate import migrate
from hare.migrations.api.plan import plan
from hare.migrations.api.sqlmigrate import sqlmigrate
from hare.migrations.api.squashmigrations import squashmigrations
from hare.migrations.making.migration_changes import MigrationChanges
from hare.migrations.making.squashed_migration import SquashedMigration

__all__ = [
    "MigrationChanges",
    "SquashedMigration",
    "checkmigrations",
    "makemigrations",
    "migrate",
    "plan",
    "squashmigrations",
    "sqlmigrate",
]
