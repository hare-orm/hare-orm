from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import MigrationAutodetectorError
from hare.migrations.making.declared_attribute_values import DeclaredAttributeValues
from hare.migrations.migration import Migration

if TYPE_CHECKING:
    from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
    from hare.migrations.state.state import State
    from hare.migrations.writer.migration_writer import MigrationWriter


class MigrationChecks:
    """The checks ``makemigrations`` runs on the migrations it made, before any is written: applied,
    they reach the models - a second run finds nothing left to do; and each file reads back as the
    very operations it was written from."""

    @staticmethod
    async def raise_if_not_converged(
        autodetector: MigrationAutodetector, project_state: State, ordered_writers: list[MigrationWriter]
    ) -> None:
        """Applies the new migrations to the state the existing ones leave and diffs it with the models
        again.

        Args:
            autodetector: The autodetector that made the migrations.
            project_state: The state the existing migrations leave - left as it is.
            ordered_writers: The new migrations, each after the new ones it depends on.

        Raises:
            MigrationAutodetectorError: The models still differ from the state the migrations leave.
        """
        state = project_state.clone()
        for writer in ordered_writers:
            await Migration(writer.name, writer.app_label, operations=writer.operations).apply(state, dry_run=True)
        new_state = autodetector.current_state(tracked_state=project_state)
        connection_by_app = {
            app_label: config.get("default_connection") for app_label, config in autodetector.apps_config.items()
        }
        left_operations = OperationGenerator(state, new_state, connection_by_app).generate(
            app_labels=[
                app_label
                for app_label in autodetector.target_app_labels
                if autodetector.apps_config[app_label].get("migrations")
            ]
        )
        if left_operations:
            described = "; ".join(operation.describe() for operation in left_operations)
            raise MigrationAutodetectorError(
                f"The new migrations don't bring the migration state to the models - a second makemigrations "
                f"would still make: {described}"
            )

    @staticmethod
    def raise_if_not_read_back(writer: MigrationWriter) -> None:
        """Reads a new migration back from the text of its file and compares its operations with the
        ones it was written from.

        Args:
            writer: The migration.

        Raises:
            MigrationAutodetectorError: An operation reads back otherwise - an argument it isn't written
                with (``deconstruct()``), or a value written otherwise than it is.
        """
        migration = Migration.from_source(writer.as_string(), name=writer.name, app_label=writer.app_label)
        for written_operation, read_operation in zip(writer.operations, migration.operations, strict=False):
            difference = MigrationChecks.get_first_difference(
                DeclaredAttributeValues.get(written_operation),
                DeclaredAttributeValues.get(read_operation),
                "operation",
            )
            if difference is not None:
                raise MigrationAutodetectorError(
                    f"Migration {writer.app_label}.{writer.name}: {written_operation.describe()} reads back from "
                    f"its file otherwise - {difference}"
                )
        if len(writer.operations) != len(migration.operations):
            raise MigrationAutodetectorError(
                f"Migration {writer.app_label}.{writer.name} reads back from its file with "
                f"{len(migration.operations)} operations, written with {len(writer.operations)}"
            )

    @staticmethod
    def get_first_difference(written: object, read: object, path: str) -> str | None:
        """Where two comparable values first differ.

        Args:
            written: The value as written.
            read: The value as read back.
            path: Where the values are.

        Returns:
            ``<path>: written <value>, read <value>``, None for equal values.
        """
        if isinstance(written, dict) and isinstance(read, dict):
            for key in sorted({*written, *read}):
                difference = MigrationChecks.get_first_difference(
                    written.get(key, "<missing>"), read.get(key, "<missing>"), f"{path}.{key}"
                )
                if difference is not None:
                    return difference
            return None
        if isinstance(written, list) and isinstance(read, list) and len(written) == len(read):
            for position, (written_item, read_item) in enumerate(zip(written, read, strict=True)):
                difference = MigrationChecks.get_first_difference(written_item, read_item, f"{path}[{position}]")
                if difference is not None:
                    return difference
            return None
        if written == read:
            return None
        return f"{path}: written {written!r}, read {read!r}"
