"""The `hare` CLI's migration-related command implementations: init, makemigrations,
squashmigrations, migrate, history, heads, sqlmigrate."""

import argparse
from typing import Any

from hare.cli.colors import TerminalColors
from hare.cli.constants import MIGRATION_GRAPH_ERRORS
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.output import OutputFormatter
from hare.core.config import HareConfig
from hare.core.connections import Connections
from hare.exceptions import (
    ConfigurationError,
    DatabaseError,
)
from hare.migrations.api import (
    makemigrations as makemigrations_api,
    migrate as migrate_api,
    sqlmigrate as sqlmigrate_api,
    squashmigrations as squashmigrations_api,
)
from hare.migrations.constants import LATEST_MIGRATION
from hare.migrations.exceptions import CircularDependencyError, PartiallyAppliedMigrationError
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.migrations_modules import MigrationsModules
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.writer.migration_writer import MigrationWriter


class MigrationCommands:
    """Implements every migration-related `hare` subcommand, sharing `CommandContext`'s config
    loading/app selection/error-boundary infrastructure with the CLI's other command classes."""

    @staticmethod
    async def init(ctx: CLIContext, app_labels: tuple[str, ...]) -> None:
        config = CommandContext.load_config(ctx)
        apps_config = CommandContext.select_apps(config, app_labels or None)
        try:
            MigrationsModules.check_unique({label: app.to_dict() for label, app in config.apps.items()})
            for label, app_config in apps_config.items():
                module, package_path = MigrationsModules.ensure_package(label, app_config.to_dict())
                print(f"{label}: {module} -> {package_path}")
        except ConfigurationError as exc:
            raise CLIError(str(exc)) from None

    @staticmethod
    async def makemigrations(
        ctx: CLIContext,
        app_labels: tuple[str, ...],
        empty: bool,
        name: str | None,
        *,
        check: bool = False,
        dry_run: bool = False,
        merge: bool = False,
    ) -> int | None:
        if empty and not app_labels:
            raise CLIUsageError("--empty requires at least one APP_LABEL")
        if empty and (check or dry_run):
            raise CLIUsageError("--empty cannot be combined with --check/--dry-run")
        if merge and empty:
            raise CLIUsageError("--merge cannot be combined with --empty")
        if merge and not app_labels:
            raise CLIUsageError("--merge requires at least one APP_LABEL")
        hare_config = CommandContext.load_config(ctx)
        MigrationCommands.raise_for_unknown_app_labels(hare_config, app_labels)
        try:
            changes = await makemigrations_api(
                config=hare_config, app_labels=list(app_labels) or None, empty=empty, merge=merge, name=name
            )
        except (ConfigurationError, *MIGRATION_GRAPH_ERRORS) as exc:
            raise CLIError(str(exc)) from None
        if changes.warnings:
            print(
                f"{TerminalColors.YELLOW}WARNING:{TerminalColors.RESET} possible unrecognized rename(s) - verify "
                "before applying:"
            )
            for warning in changes.warnings:
                print(f"    {TerminalColors.DIM}{warning}{TerminalColors.RESET}")
            print()
        if changes.data_loss_warnings:
            print(f"{TerminalColors.YELLOW}WARNING:{TerminalColors.RESET} data loss risk - verify before applying:")
            for warning in changes.data_loss_warnings:
                print(f"    {TerminalColors.DIM}{warning}{TerminalColors.RESET}")
            print()
        if not changes.writers:
            print(f"{TerminalColors.DIM}No changes detected{TerminalColors.RESET}")
            return None
        if check or dry_run:
            for writer in changes.writers:
                print(f"{TerminalColors.CYAN}{writer.app_label}.{writer.name}{TerminalColors.RESET}")
                for operation in writer.operations:
                    print(f"    - {operation.describe()}")
            return 1 if check else None
        written_paths = []
        for writer in changes.writers:
            written_path = writer.write()
            written_paths.append(written_path)
            print(f"  {TerminalColors.GREEN}Created{TerminalColors.RESET} {writer.app_label}.{writer.name}")
            print(f"    {TerminalColors.DIM}{written_path}{TerminalColors.RESET}")
        MigrationWriter.format_files(written_paths)
        return None

    @staticmethod
    async def squashmigrations(ctx: CLIContext, app_label: str, name: str | None) -> None:
        """Collapses every migration of one app into a single new one - see
        ``hare.migrations.api.squashmigrations()``."""
        hare_config = CommandContext.load_config(ctx)
        MigrationCommands.raise_for_unknown_app_labels(hare_config, (app_label,))
        try:
            squashed = await squashmigrations_api(config=hare_config, app_label=app_label, name=name)
        except (ConfigurationError, *MIGRATION_GRAPH_ERRORS) as exc:
            raise CLIError(str(exc)) from None
        writer = squashed.writer
        if writer is None:
            print(
                f"{TerminalColors.DIM}Only one migration ({squashed.replaced_names[0]}) for '{app_label}' - "
                f"nothing to squash{TerminalColors.RESET}"
            )
            return
        written_path = writer.write()
        MigrationWriter.format_files([written_path])
        print(f"  {TerminalColors.GREEN}Created{TerminalColors.RESET} {writer.app_label}.{writer.name}")
        print(f"    {TerminalColors.DIM}{written_path}{TerminalColors.RESET}")
        print()
        print(
            f"{TerminalColors.DIM}Replaces {len(squashed.replaced_names)} existing migration(s):{TerminalColors.RESET}"
        )
        for old_name in squashed.replaced_names:
            print(f"    {TerminalColors.DIM}{old_name}{TerminalColors.RESET}")
        print()
        print(
            f"{TerminalColors.YELLOW}NOTE:{TerminalColors.RESET} `replaces` is written for documentation only - the "
            "migration loader/executor do not read it back. Safe ONLY for a fresh database, or one "
            f"where '{app_label}' migrations were never applied. Verify the new migration (diff "
            "against the schema the old ones produced, run the full test suite), then delete the "
            "old migration files by hand."
        )
        if squashed.data_migration_names:
            print()
            print(
                f"{TerminalColors.YELLOW}WARNING:{TerminalColors.RESET} the following migration(s) contain a "
                "RunPython/RunSQL operation - its effect is NOT carried over into the squashed migration. "
                "Re-add it to the new migration by hand if the squashed history still needs to apply it:"
            )
            for data_migration_name in squashed.data_migration_names:
                print(f"    {TerminalColors.DIM}{data_migration_name}{TerminalColors.RESET}")

    @staticmethod
    def raise_for_unknown_app_labels(hare_config: HareConfig, app_labels: tuple[str, ...]) -> None:
        """Refuses app labels the configuration doesn't have.

        Args:
            hare_config: The configuration.
            app_labels: The labels given on the command line.

        Raises:
            CLIUsageError: A label names no configured app.
        """
        for app_label in app_labels:
            if app_label not in hare_config.apps:
                raise CLIUsageError(f"Unknown app label {app_label}")

    @staticmethod
    def get_apps_with_existing_modules(apps_config: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The apps with their migrations modules resolved - see
        ``MigrationsModules.get_apps_with_existing_modules()``.

        Raises:
            CLIError: Two apps share a migrations module.
        """
        try:
            return MigrationsModules.get_apps_with_existing_modules(apps_config)
        except ConfigurationError as exc:
            raise CLIError(str(exc)) from None

    @staticmethod
    def _split_dotted_app_label(app_label: str | None, migration: str | None) -> tuple[str | None, str | None]:
        """Splits a bare "app.migration"-style positional app_label into its two parts, when no
        separate migration argument was already given."""
        if app_label and not migration and "." in app_label:
            split_app_label, split_migration = app_label.split(".", 1)
            return split_app_label, split_migration
        return app_label, migration

    @staticmethod
    async def _run_migrate(
        ctx: CLIContext,
        app_label: str | None,
        migration: str | None,
        *,
        fake: bool,
        dry_run: bool,
    ) -> None:
        hare_config = CommandContext.load_config(ctx)
        # normalize_apps finds each app's "migrations" module - without it an app would seem to have
        # none.
        apps_config = CommandContext.select_apps(hare_config, None)
        apps_dict = MigrationCommands.get_apps_with_existing_modules(
            {label: app.to_dict() for label, app in apps_config.items()}
        )
        config_dict = hare_config.to_dict()
        config_dict["apps"] = apps_dict

        target: str | None = None
        app_label, migration = MigrationCommands._split_dotted_app_label(app_label, migration)
        if app_label and app_label not in apps_config:
            raise CLIUsageError(f"Unknown app label {app_label}")
        if app_label and not migration:
            target = f"{app_label}.{LATEST_MIGRATION}"
        elif migration:
            if not app_label:
                raise CLIUsageError("MIGRATION requires APP_LABEL")
            target = f"{app_label}.{migration}"

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict):
            try:
                try:
                    await migrate_api(
                        config=config_dict,
                        app_labels=None,
                        target=target,
                        fake=fake,
                        dry_run=dry_run,
                        reporter=OutputFormatter.emit_migration_plan,
                        progress=(
                            OutputFormatter.dry_run_progress_reporter if dry_run else OutputFormatter.progress_reporter
                        ),
                    )
                except BaseException:
                    OutputFormatter.finish_open_progress_line()
                    raise
            except (CircularDependencyError, PartiallyAppliedMigrationError) as exc:
                raise CLIError(str(exc)) from None
            except ConfigurationError as exc:
                raise CLIError(str(exc)) from None
            except DatabaseError:
                # Left for the enclosing `database_error_boundary()` to translate into its own,
                # friendlier "Could not connect to the database" message - must not be caught by
                # the catch-all below first.
                raise
            except Exception as exc:
                # Anything else raised while applying - no raw traceback.
                raise CLIError(str(exc)) from exc

    @staticmethod
    async def migrate(
        ctx: CLIContext,
        app_label: str | None,
        migration: str | None,
        fake: bool,
        dry_run: bool,
    ) -> None:
        await MigrationCommands._run_migrate(ctx, app_label, migration, fake=fake, dry_run=dry_run)

    @staticmethod
    async def history(ctx: CLIContext, app_labels: tuple[str, ...]) -> None:
        hare_config = CommandContext.load_config(ctx)
        selected_apps_config = CommandContext.select_apps(hare_config, app_labels or None)
        selected_apps_dict = {label: app.to_dict() for label, app in selected_apps_config.items()}
        apps_by_connection = CommandContext.group_apps_by_connection(selected_apps_dict)

        # Every configured app is initialized, not just the selected ones - a selected app's
        # relations can point at models of an unselected app.
        config_dict = hare_config.to_dict()

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict):
            for connection_name, subset in apps_by_connection.items():
                recorder = MigrationRecorder(Connections.get(connection_name))
                applied = await recorder.applied_migrations()
                OutputFormatter.emit_history(applied, connection_name, subset)

    @staticmethod
    async def heads(ctx: CLIContext, app_labels: tuple[str, ...]) -> None:
        hare_config = CommandContext.load_config(ctx)
        # Every configured app - a migration may depend on an unselected app's.
        full_apps_config = CommandContext.select_apps(hare_config, None)
        # normalize_apps resolves the "migrations" module for every app that didn't set one
        # explicitly - without it, MigrationLoader.load_disk() treats that app as unmigrated
        # and reports "(no heads)" even when migrations genuinely exist on disk.
        full_apps_dict = MigrationCommands.get_apps_with_existing_modules(
            {label: app.to_dict() for label, app in full_apps_config.items()}
        )

        apps_config = CommandContext.select_apps(hare_config, app_labels or None)
        apps_dict = {label: app.to_dict() for label, app in apps_config.items()}
        apps_by_connection = CommandContext.group_apps_by_connection(apps_dict)

        loader = MigrationLoader(full_apps_dict, NoopRecorder(), load=False)
        try:
            await loader.build_graph()
        except MIGRATION_GRAPH_ERRORS as exc:
            # Mirrors migrate()'s own translation - a circular dependency, a dangling
            # dependency on a removed migration or a broken migration file is shown as an
            # error, not a raw traceback.
            raise CLIError(str(exc)) from None

        for connection_name, subset in apps_by_connection.items():
            OutputFormatter.emit_heads(loader, connection_name, subset)

    @staticmethod
    async def sqlmigrate_cmd(
        ctx: CLIContext,
        app_label: str | None,
        migration_name: str | None,
        backward: bool,
    ) -> None:
        config = CommandContext.load_config(ctx)
        if not app_label or not migration_name:
            labels = sorted(config.apps) if config.apps else []
            available = ", ".join(labels) if labels else "(none)"
            if not app_label:
                raise CLIUsageError(f"app_label is required. Available app labels: {available}")
            raise CLIUsageError(f"migration_name is required. Usage: sqlmigrate {app_label} <migration_name>")

        # normalize_apps resolves the "migrations" module for every app that didn't set one
        # explicitly - without it, sqlmigrate fails with "Cannot find migration" for a
        # migration that genuinely exists on disk.
        apps_config = CommandContext.select_apps(config, None)
        if app_label not in apps_config:
            raise CLIUsageError(f"Unknown app label {app_label}")
        apps_dict = MigrationCommands.get_apps_with_existing_modules(
            {label: app.to_dict() for label, app in apps_config.items()}
        )
        config_dict = config.to_dict()
        config_dict["apps"] = apps_dict

        try:
            statements = await sqlmigrate_api(
                config=config_dict,
                app_label=app_label,
                migration_name=migration_name,
                backward=backward,
            )
        except MIGRATION_GRAPH_ERRORS as exc:
            # Mirrors migrate()'s/heads()'s own translation - no raw traceback for the user.
            raise CLIError(str(exc)) from None

        if not statements:
            print(f"{TerminalColors.DIM}-- (no SQL statements){TerminalColors.RESET}")
            return

        # sqlmigrate already includes BEGIN;/COMMIT; when the migration runs atomically.
        for statement in statements:
            if statement.startswith("--") or statement in ("BEGIN;", "COMMIT;"):
                print(f"{TerminalColors.DIM}{statement}{TerminalColors.RESET}")
            else:
                if not statement.rstrip().endswith(";"):
                    print(f"{statement};")
                else:
                    print(statement)

    @staticmethod
    async def _run_init(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.init(ctx, tuple(args.app_labels))

    @staticmethod
    async def _run_makemigrations(ctx: CLIContext, args: argparse.Namespace) -> int | None:
        return await MigrationCommands.makemigrations(
            ctx,
            tuple(args.app_labels),
            args.empty,
            args.name,
            check=args.check,
            dry_run=args.dry_run,
            merge=args.merge,
        )

    @staticmethod
    async def _run_squashmigrations(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.squashmigrations(ctx, args.app_label, args.name)

    @staticmethod
    async def _run_migrate_cmd(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.migrate(ctx, args.app_label, args.migration, args.fake, args.dry_run)

    @staticmethod
    async def _run_history(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.history(ctx, tuple(args.app_labels))

    @staticmethod
    async def _run_heads(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.heads(ctx, tuple(args.app_labels))

    @staticmethod
    async def _run_sqlmigrate(ctx: CLIContext, args: argparse.Namespace) -> None:
        await MigrationCommands.sqlmigrate_cmd(ctx, args.app_label, args.migration_name, args.backward)
