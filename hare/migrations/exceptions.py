from __future__ import annotations

from hare.exceptions import HareError


class HareMigrationError(HareError):
    """Base migration error."""


class UnknownMigrationError(HareMigrationError, LookupError):
    """Raised when an app label, a migration or a migration target names nothing that exists."""


class IrreversibleMigrationError(HareMigrationError):
    """Raised when a migration is unapplied while one of its operations can't be reversed."""


class IncompatibleStateError(HareMigrationError):
    """Raised when a migration operation can't be executed on a given State."""


class CircularDependencyError(HareMigrationError):
    """Raised when the migration dependency graph contains a cycle."""


class MigrationLoadError(HareMigrationError):
    """Raised when a migration file on disk can't be imported."""


class PartiallyAppliedMigrationError(HareMigrationError):
    """A non-atomic migration (``atomic = False``) failed partway: some of its changes may have landed,
    but it isn't recorded as applied, and running ``migrate`` again would replay every operation.
    Reconcile the schema by hand, then fix forward with a new migration or mark this one applied
    with ``migrate --fake``.
    """


class FieldNarrowingDataLossError(HareMigrationError):
    """An ``AlterField`` narrowing ``max_length``, ``max_digits``/``decimal_places`` or the like would
    truncate or round existing rows - the cast does it without an error. Clean up or widen the
    values first.
    """


class ForeignKeyTargetChangeError(HareMigrationError):
    """An ``AlterField`` points a relation at another target column (``to_field``) while rows still
    store key values of the old one. Migrate by hand: add a new relation field, fill it with
    ``RunPython``/``RunSQL``, remove the old one.
    """


class InconsistentMigrationStateError(HareMigrationError, RuntimeError):
    """Raised when replaying the migration files leaves a relation pointing at a model that no
    migration creates (or that one has already deleted) - the files themselves are inconsistent
    and need fixing by hand before any command can build the migration state from them.
    """
