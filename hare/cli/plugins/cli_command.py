from __future__ import annotations

import abc
import argparse
from typing import ClassVar

from hare.cli.context.cli_context import CLIContext


class CLICommand(abc.ABC):
    """A `hare` subcommand contributed by an installed package or by the project itself.

    The module defining a command is imported on every `hare` run, so keep it light and import
    heavy dependencies inside ``run()``.
    """

    #: Subcommand name, e.g. "admin" for `hare admin`.
    name: ClassVar[str]
    #: One-line description shown in `hare --help`.
    help: ClassVar[str] = ""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declares the subcommand's own arguments.

        Args:
            parser: The subcommand's parser.
        """

    @abc.abstractmethod
    async def run(self, ctx: CLIContext, args: argparse.Namespace) -> int | None:
        """Runs the subcommand.

        Args:
            ctx: The global `-c`/`--config-file` options; pass it to ``CommandContext`` helpers.
            args: The parsed arguments.

        Returns:
            The process exit code, or None for 0.

        Raises:
            CLIUsageError: For bad arguments (exit code 2).
            CLIError: For any other failure to report (exit code 1).
        """
