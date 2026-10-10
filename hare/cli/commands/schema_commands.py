"""The `hare` CLI's schema-inspection command implementations: inspectdb, drift."""

from __future__ import annotations

import argparse

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.output.output_formatter import OutputFormatter
from hare.cli.output.terminal_colors import TerminalColors
from hare.core.connections.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.inspectdb import SchemaInspector
from hare.migrations.drift.detect_drift_for_alias import detect_drift_for_alias


class SchemaCommands:
    """Implements inspectdb/drift, sharing `CommandContext`'s config loading/app selection/
    error-boundary infrastructure with the CLI's other command classes."""

    @staticmethod
    async def inspectdb(
        cli_context: CLIContext, connection_alias: str | None, tables: tuple[str, ...], schema: str | None = None
    ) -> None:
        """Prints hare-orm model source reverse-engineered from an existing database's schema -
        best-effort, review before trusting it, same as Django's own inspectdb."""
        hare_config = CommandContext.load_config(cli_context)
        if connection_alias:
            if connection_alias not in hare_config.connections:
                raise CLIUsageError(f"Unknown connection alias: {connection_alias}")
            inspected_connection_alias = connection_alias
        else:
            # HareConfig itself already rejects an empty `connections` dict at config-load time
            # (see HareConfig.__post_init__), so `hare_config.connections` is guaranteed non-empty
            # here - next(iter(...)) can't actually raise StopIteration in practice.
            inspected_connection_alias = next(iter(hare_config.connections))
        config_dict = hare_config.to_dict()
        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict):
            connection = Connections.get(inspected_connection_alias)
            source = await SchemaInspector.inspect(connection, tables=list(tables) or None, schema=schema)
            print(source)

    @staticmethod
    async def drift(
        cli_context: CLIContext, connection_alias: str | None, app_labels: tuple[str, ...], schema: str | None
    ) -> int | None:
        """Compares the live database with the current models for every app on the checked connection,
        by the diffing ``makemigrations`` uses with the database as the old state. Reports only -
        generates no source.

        Returns:
            1 if they disagree, None otherwise.
        """
        hare_config = CommandContext.load_config(cli_context)
        # Every configured app - a model may relate to an unselected app's.
        full_apps_config = CommandContext.select_apps(hare_config, None)
        apps_dict = {label: app.to_dict() for label, app in full_apps_config.items()}
        config_dict = hare_config.to_dict()
        config_dict["apps"] = apps_dict

        if connection_alias:
            if connection_alias not in hare_config.connections:
                raise CLIUsageError(f"Unknown connection alias: {connection_alias}")
            checked_connection_alias = connection_alias
        else:
            checked_connection_alias = next(iter(hare_config.connections))

        requested_apps_config = CommandContext.select_apps(hare_config, app_labels or None)
        target_labels = [
            label
            for label in requested_apps_config
            if apps_dict[label].get("default_connection", DEFAULT_CONNECTION_NAME) == checked_connection_alias
        ]
        if not target_labels:
            raise CLIUsageError(f"No configured app uses connection '{checked_connection_alias}'")

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(config_dict) as context:
            if not context.apps:
                raise CLIError("Hare apps are not initialized")
            result = await detect_drift_for_alias(
                context.apps, apps_dict, checked_connection_alias, schema=schema, app_labels=target_labels
            )

        if not result.has_drift:
            return None

        OutputFormatter.echo_connection_header(checked_connection_alias)
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
    async def run_inspectdb(cli_context: CLIContext, args: argparse.Namespace) -> None:
        await SchemaCommands.inspectdb(cli_context, args.connection_alias, tuple(args.tables), args.schema)

    @staticmethod
    async def run_drift(cli_context: CLIContext, args: argparse.Namespace) -> int | None:
        return await SchemaCommands.drift(cli_context, args.connection_alias, tuple(args.app_labels), args.schema)
