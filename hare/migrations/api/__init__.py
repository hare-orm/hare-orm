"""The migration commands as functions: make, squash, plan and apply migrations, show their SQL."""

from hare.migrations.api.makemigrations import makemigrations
from hare.migrations.api.migrate import migrate
from hare.migrations.api.migration_changes import MigrationChanges
from hare.migrations.api.plan import plan
from hare.migrations.api.sqlmigrate import sqlmigrate
from hare.migrations.api.squashed_migration import SquashedMigration
from hare.migrations.api.squashmigrations import squashmigrations

__all__ = [
    "MigrationChanges",
    "SquashedMigration",
    "makemigrations",
    "migrate",
    "plan",
    "squashmigrations",
    "sqlmigrate",
]
