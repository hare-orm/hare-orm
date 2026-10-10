from __future__ import annotations

from typing import Any, cast

from hare.exceptions import ConfigurationError
from hare.migrations.constants import MIGRATION_NUMBER_RE
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.making.migration_optimizer import MigrationOptimizer
from hare.migrations.making.squashed_migration import SquashedMigration
from hare.migrations.migration import Migration
from hare.migrations.operations.operation import Operation
from hare.migrations.writer.migration_writer import MigrationWriter


class MigrationSquasher:
    """Squashes a run of an app's migrations into one: their operations in order, shortened by
    ``MigrationOptimizer``, without the ``elidable`` ``RunPython``/``RunSQL``. The new migration
    names what it replaces in ``replaces`` - the loader then puts it in their place where none or
    all of them are applied, and leaves it out until all are."""

    def __init__(self, apps_config: dict[str, dict[str, Any]]) -> None:
        """
        Args:
            apps_config: Every configured app, with its migrations module.
        """
        self.apps_config = apps_config

    async def squash(
        self, app_label: str, end_name: str, *, start_name: str | None = None, squashed_name: str | None = None
    ) -> SquashedMigration:
        """Squashes an app's migrations from ``start_name`` (its first one without it) to ``end_name``.

        Args:
            app_label: The app.
            end_name: The last migration squashed - its name or a prefix of it.
            start_name: The first migration squashed - its name or a prefix of it; the app's first
                migration when None.
            squashed_name: The new migration's name after its number; ``squashed_<end>`` when None.

        Returns:
            The squashed migration, not written yet - without a writer when the range holds one
            migration.

        Raises:
            ConfigurationError: An unknown app or migration, a start after the end, or a new name
                one of the app's migrations already has.
        """
        if app_label not in self.apps_config:
            raise ConfigurationError(f"Unknown app label: {app_label}")
        loader = MigrationLoader(self.apps_config, NoopRecorder())
        await loader.build_graph()
        end_key = self.get_migration_key(loader, app_label, end_name)
        plan = [key for key in loader.graph.forwards_plan(end_key) if key.app_label == app_label]
        if start_name is not None:
            start_key = self.get_migration_key(loader, app_label, start_name)
            if start_key not in plan:
                raise ConfigurationError(f"{start_key} doesn't come before {end_key} - nothing to squash between them")
            plan = plan[plan.index(start_key) :]
        if len(plan) == 1:
            return SquashedMigration(writer=None, replaced_names=[plan[0].name])

        migrations = [cast("Migration", loader.graph.nodes[key]) for key in plan]
        replaced: list[tuple[str, str]] = []
        for key, migration in zip(plan, migrations, strict=True):
            replaced.extend(migration.replaces or [(key.app_label, key.name)])
        replaced_keys = {MigrationKey(app_label=label, name=name) for label, name in replaced} | set(plan)
        operations: list[Operation] = []
        elided_operation_count = 0
        dependencies: list[Any] = []
        run_before: list[tuple[str, str]] = []
        for migration in migrations:
            for operation in migration.operations:
                if getattr(operation, "elidable", False):
                    elided_operation_count += 1
                    continue
                operations.append(operation)
            for dependency in migration.dependencies:
                if MigrationKey(app_label=dependency[0], name=dependency[1]) in replaced_keys:
                    continue
                if dependency not in dependencies:
                    dependencies.append(dependency)
            for successor in migration.run_before:
                if (
                    MigrationKey(app_label=successor[0], name=successor[1]) not in replaced_keys
                    and successor not in run_before
                ):
                    run_before.append(successor)

        name = self.get_squashed_name(loader, plan, squashed_name)
        writer = MigrationWriter(
            name,
            app_label,
            MigrationOptimizer(app_label).optimize(operations),
            dependencies=dependencies,
            run_before=run_before,
            replaces=replaced,
            initial=not any(dependency[0] == app_label for dependency in dependencies),
            migrations_module=self.apps_config[app_label]["migrations"],
            atomic=all(migration.atomic for migration in migrations if isinstance(migration, Migration)),
        )
        return SquashedMigration(
            writer=writer,
            replaced_names=[name for _label, name in replaced],
            elided_operation_count=elided_operation_count,
            squashed_operation_count=len(operations),
        )

    @staticmethod
    def get_migration_key(loader: MigrationLoader, app_label: str, name: str) -> MigrationKey:
        """The migration of an app named, or named by a unique prefix.

        Args:
            loader: The loaded migrations.
            app_label: The app.
            name: The name or a prefix of it.

        Returns:
            The migration's key.

        Raises:
            ConfigurationError: No migration or more than one has the name.
        """
        exact_key = MigrationKey(app_label=app_label, name=name)
        if exact_key in loader.graph.nodes:
            return exact_key
        matching_keys = [key for key in loader.graph.nodes if key.app_label == app_label and key.name.startswith(name)]
        if len(matching_keys) == 1:
            return matching_keys[0]
        if not matching_keys:
            raise ConfigurationError(f"No migration of '{app_label}' is named {name!r}")
        raise ConfigurationError(
            f"More than one migration of '{app_label}' starts with {name!r}: "
            f"{', '.join(sorted(key.name for key in matching_keys))}"
        )

    @staticmethod
    def get_squashed_name(loader: MigrationLoader, plan: list[MigrationKey], squashed_name: str | None) -> str:
        """The squashed migration's name: the first squashed migration's number and the given name,
        ``squashed_<last squashed migration>`` without one.

        Args:
            loader: The loaded migrations.
            plan: The squashed migrations, in order.
            squashed_name: The given name.

        Returns:
            The name.

        Raises:
            ConfigurationError: One of the app's migrations already has it.
        """
        number_match = MIGRATION_NUMBER_RE.match(plan[0].name)
        number = int(number_match.group(1)) if number_match else 1
        name = MigrationWriter.format_name(number, squashed_name or f"squashed_{plan[-1].name}")
        if MigrationKey(app_label=plan[0].app_label, name=name) in loader.disk_migrations:
            raise ConfigurationError(
                f"'{plan[0].app_label}' already has a migration named {name} - give the squashed one another name"
            )
        return name
