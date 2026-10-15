"""Extension point for adding subcommands to the `hare` CLI from installed packages and projects."""

from __future__ import annotations

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.plugins.cli_command import CLICommand
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.cli.plugins.loaded_cli_command import LoadedCLICommand

__all__ = [
    "CLICommand",
    "CLICommandRegistry",
    "CLIContext",
    "CLIError",
    "CLIUsageError",
    "CommandContext",
    "LoadedCLICommand",
]
