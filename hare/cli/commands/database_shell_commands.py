"""The `hare` CLI's dbshell command: the database's own interactive client on a configured connection."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.core.connections.connections import Connections
from hare.exceptions import UnSupportedError


class DatabaseShellCommands:
    """Implements dbshell - the interactive client the connection's
    dialect names (``DatabaseClient.get_shell_command()``)."""

    @staticmethod
    async def dbshell(cli_context: CLIContext, connection_alias: str | None) -> int | None:
        """Runs the database's interactive client on a connection until the user leaves it.

        Returns:
            The client's exit code, None for 0.

        Raises:
            CLIUsageError: The connection isn't configured.
            CLIError: The dialect names no client, the database can't be opened by one, or the
                client isn't installed.
        """
        hare_config = CommandContext.load_config(cli_context)
        if connection_alias is not None and connection_alias not in hare_config.connections:
            raise CLIUsageError(f"Unknown connection alias: {connection_alias}")
        opened_connection_alias = connection_alias or next(iter(hare_config.connections))
        async with CommandContext.database_error_boundary(), CommandContext.hare_cli_context(hare_config.to_dict()):
            client = Connections.get(opened_connection_alias)
            try:
                command = await client.get_shell_command()
            except UnSupportedError as error:
                raise CLIError(str(error)) from None
        if command is None:
            raise CLIError(f"The {client.dialect} dialect names no interactive client for hare dbshell")
        program = shutil.which(command.arguments[0])
        if program is None:
            raise CLIError(f"{command.arguments[0]} is not installed or not on PATH - hare dbshell runs it")
        # The client takes over the terminal: it reads this process's stdin and writes its stdout.
        process = await asyncio.create_subprocess_exec(
            program, *command.arguments[1:], env={**os.environ, **command.environment}
        )
        return await process.wait() or None

    @staticmethod
    async def run_dbshell(cli_context: CLIContext, args: argparse.Namespace) -> int | None:
        return await DatabaseShellCommands.dbshell(cli_context, args.connection_alias)
