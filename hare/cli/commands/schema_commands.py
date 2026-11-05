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
            inspected_connection_alias = next(iter(hare_config.connections))
        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(hare_config):
            connection = Connections.get(inspected_connection_alias)
            source = await SchemaInspector.inspect(connection, tables=list(tables) or None, schema=schema)
            print(source)

    @staticmethod
    async def drift(
        cli_context: CLIContext,
        connection_alias: str | None,
        app_labels: tuple[str, ...],
        schema: str | None,
        journal_tables: tuple[str, ...] = (),
    ) -> int | None:
        """Compares the live catalog with a shadow schema generated from the current models of the
        selected apps. Reports differences without writing migrations.

        Returns:
            1 if they disagree, None otherwise.
        """
        hare_config = CommandContext.load_config(cli_context)
        # Every configured app - a model may relate to an unselected app's.
        full_apps_config = CommandContext.select_apps(hare_config, None)
        apps_dict = {label: app.to_dict() for label, app in full_apps_config.items()}

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
            raise CLIUsageError(f"No configured app uses connection {checked_connection_alias!r}")

        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(hare_config) as context:
            if not context.apps:
                raise CLIError("Hare apps are not initialized")
            result = await detect_drift_for_alias(
                context.apps,
                apps_dict,
                checked_connection_alias,
                schema=schema,
                app_labels=target_labels,
                journal_tables=journal_tables,
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
        for mismatch in result.mismatched_objects:
            print(
                f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} {mismatch.part_type} {mismatch.name!r} "
                f"in {mismatch.table or 'schema'!r}: {mismatch.detail}"
            )
        for table_name in result.untracked_tables:
            print(f"  {TerminalColors.YELLOW}~{TerminalColors.RESET} Untracked table in database: {table_name!r}")
        return 1

    @staticmethod
    async def run_inspectdb(cli_context: CLIContext, args: argparse.Namespace) -> None:
        await SchemaCommands.inspectdb(cli_context, args.connection_alias, tuple(args.tables), args.schema)

    @staticmethod
    async def run_drift(cli_context: CLIContext, args: argparse.Namespace) -> int | None:
        return await SchemaCommands.drift(
            cli_context, args.connection_alias, tuple(args.app_labels), args.schema, tuple(args.journal_tables)
        )
