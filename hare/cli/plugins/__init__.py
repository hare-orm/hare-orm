"""Extension point for adding subcommands to the `hare` CLI from installed packages and projects."""

from __future__ import annotations

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.plugins.cli_command import CLICommand

__all__ = [
    "CLICommand",
    "CLIContext",
    "CLIError",
    "CLIUsageError",
    "CommandContext",
]
