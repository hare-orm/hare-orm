from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from hare.cli.context.cli_context import CLIContext

CommandHandler = Callable[[CLIContext, argparse.Namespace], Awaitable[int | None]]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Execution metadata kept outside the namespace populated by command arguments."""

    handler: CommandHandler
    context: CLIContext
    arguments: argparse.Namespace
