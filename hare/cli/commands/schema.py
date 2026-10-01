"""The `hare` CLI's schema-inspection command implementations: inspectdb, drift."""

import argparse

from hare.cli.colors import TerminalColors
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.output import OutputFormatter
from hare.core.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.inspectdb import SchemaInspector
from hare.migrations.drift.detect_drift_for_alias import detect_drift_for_alias


class SchemaCommands:
    """Implements inspectdb/drift, sharing `CommandContext`'s config loading/app selection/
    error-boundary infrastructure with the CLI's other command classes."""

    @staticmethod
    async def inspectdb(
        ctx: CLIContext, connection_name: str | None, tables: tuple[str, ...], schema: str | None = None
    ) -> None:
        """Prints hare-orm model source reverse-engineered from an existing database's schema -
        best-effort, review before trusting it, same as Django's own inspectdb."""
        hare_config = CommandContext.load_config(ctx)
        if connection_name:
            if connection_name not in hare_config.connections:
                raise CLIUsageError(f"Unknown connection alias: {connection_name}")
            alias = connection_name
        else:
            # HareConfig itself already rejects an empty `connections` dict at config-load time
            # (see HareConfig.__post_init__), so `hare_config.connections` is guaranteed non-empty
            # here - next(iter(...)) can't actually raise StopIteration in practice.
            alias = next(iter(hare_config.connections))
        config_dict = hare_config.to_dict()
        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict):
            connection = Connections.get(alias)
            source = await SchemaInspector.inspect(connection, tables=list(tables) or None, schema=schema)
            print(source)

    @staticmethod
    async def drift(
        ctx: CLIContext, connection_name: str | None, app_labels: tuple[str, ...], schema: str | None
    ) -> int | None:
        """Compares the live database with the current models for every app on the checked connection,
        by the diffing ``makemigrations`` uses with the database as the old state. Reports only -
        generates no source.

        Returns:
            1 if they disagree, None otherwise.
        """
        hare_config = CommandContext.load_config(ctx)
        # Every configured app - a model may relate to an unselected app's.
        full_apps_config = CommandContext.select_apps(hare_config, None)
        apps_dict = {label: app.to_dict() for label, app in full_apps_config.items()}
        config_dict = hare_config.to_dict()
        config_dict["apps"] = apps_dict

        if connection_name:
            if connection_name not in hare_config.connections:
                raise CLIUsageError(f"Unknown connection alias: {connection_name}")
            alias = connection_name
        else:
            alias = next(iter(hare_config.connections))

        requested_apps_config = CommandContext.select_apps(hare_config, app_labels or None)
        target_labels = [
            label
            for label in requested_apps_config
            if apps_dict[label].get("default_connection", DEFAULT_CONNECTION_NAME) == alias
        ]
        if not target_labels:
            raise CLIUsageError(f"No configured app uses connection '{alias}'")

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict) as cli_ctx:
            if not cli_ctx.apps:
                raise CLIError("Hare apps are not initialized")
            result = await detect_drift_for_alias(
                cli_ctx.apps, apps_dict, alias, schema=schema, app_labels=target_labels
            )

        if not result.has_drift:
            return None

        OutputFormatter.echo_connection_header(alias)
        for operation in result.operations:
            print(f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} {operation.describe()}")
        for app_label, model_name, table, column_name in result.untracked_columns:
            print(
                f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} Untracked column {column_name!r} in table "
                f"{table!r} ({app_label}.{model_name})"
            )
        for mismatch in result.mismatched_columns:
            print(
                f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} Column {mismatch.column!r} in table "
                f"{mismatch.table!r} "
                f"({mismatch.app_label}.{mismatch.model_name}): {mismatch.detail}"
            )
        for table_name in result.untracked_tables:
            print(f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} Untracked table in database: {table_name!r}")
        return 1

    @staticmethod
    async def _run_inspectdb(ctx: CLIContext, args: argparse.Namespace) -> None:
        await SchemaCommands.inspectdb(ctx, args.connection_name, tuple(args.tables), args.schema)

    @staticmethod
    async def _run_drift(ctx: CLIContext, args: argparse.Namespace) -> int | None:
        return await SchemaCommands.drift(ctx, args.connection_name, tuple(args.app_labels), args.schema)
