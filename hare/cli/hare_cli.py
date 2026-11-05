from __future__ import annotations

import argparse
import asyncio
import sys

from hare.cli.arg_parser_builder import ArgParserBuilder
from hare.cli.constants import CLI_INTERRUPTED_EXIT_CODE, CLI_INTERRUPTED_MESSAGE
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.messages import IPYTHON_REQUIRED, SHELL_REQUIRES_SYNC_DISPATCH
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.cli.shell_launcher import ShellLauncher
from hare.cli.shell_session import ShellSession
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import HareError


class HareCLI:
    """The ``hare`` command's entry point - argument parsing, the shell and the commands are classes of
    their own.
    """

    @staticmethod
    def shell(cli_context: CLIContext) -> None:
        """Launch the interactive shell outside an active event loop."""
        if ShellLauncher.interactive_shell_class is None:
            raise CLIError(IPYTHON_REQUIRED)
        config = CommandContext.load_config(cli_context)
        with ShellSession(CommandContext.hare_cli_context(config)) as session:
            session.launch()

    @staticmethod
    async def run_shell(cli_context: CLIContext, _args: argparse.Namespace) -> None:
        """The parser's shell marker; asynchronous callers cannot nest an interactive loop."""
        if ShellLauncher.interactive_shell_class is None:
            raise CLIError(IPYTHON_REQUIRED)
        raise CLIError(SHELL_REQUIRES_SYNC_DISPATCH)

    @staticmethod
    def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace | int:
        """Select a command independently of how it is executed."""
        global_options = ArgParserBuilder.parse_global_options(argv)
        plugin_commands = CLICommandRegistry.discover(CLIContext(config=global_options.config))
        parser = ArgParserBuilder.build_parser(plugin_commands)
        try:
            return parser.parse_args(argv)
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else 1

    @staticmethod
    def report_error(error: CLIError | HareError) -> int:
        """Print a command failure and return the CLI's corresponding exit status."""
        message = str(error) if isinstance(error, CLIError) else f"{type(error).__name__}: {error}"
        print(message, file=sys.stderr)
        return 2 if isinstance(error, CLIUsageError) else 1

    @staticmethod
    async def run_command_async(args: argparse.Namespace) -> int:
        """Run a parsed asynchronous command on the caller's loop."""
        try:
            result = await args.func(CLIContext(config=args.config), args)
        except (CLIError, HareError) as error:
            return HareCLI.report_error(error)
        return result if isinstance(result, int) else 0

    @staticmethod
    async def run_cli_async(argv: list[str] | None = None) -> int:
        """Parse and execute a noninteractive command on an existing event loop."""
        args = HareCLI.parse_arguments(argv)
        return args if isinstance(args, int) else await HareCLI.run_command_async(args)

    @staticmethod
    def run_cli(argv: list[str] | None = None) -> int:
        """Dispatch interactive sessions synchronously and other commands through asyncio."""
        args = HareCLI.parse_arguments(argv)
        if isinstance(args, int):
            return args
        if args.func is not HareCLI.run_shell:
            return asyncio.run(HareCLI.run_command_async(args))
        try:
            HareCLI.shell(CLIContext(config=args.config))
        except (CLIError, HareError) as error:
            return HareCLI.report_error(error)
        return 0

    @staticmethod
    def configure_output_encoding() -> None:
        """Force UTF-8 on redirected stdout/stderr and unencodable-character replacement on terminals."""
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is None:
                continue
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")

    @staticmethod
    def quiet_library_loggers() -> None:
        """Keeps the driver libraries' own warnings out of the output when nothing configured logging -
        Python would print them, a failed connect's traceback among them, through its last-resort
        handler beside hare's own message. A configured logging still receives them."""
        DatabaseClient.quiets_driver_loggers = True

    @staticmethod
    def main() -> None:
        """The `hare` console script: runs the CLI and exits with its code.

        Raises:
            SystemExit: Always, with the exit code.
        """
        HareCLI.configure_output_encoding()
        HareCLI.quiet_library_loggers()
        if sys.path[0] != ".":
            sys.path.insert(0, ".")
        try:
            exit_code = HareCLI.run_cli()
        except KeyboardInterrupt:
            # Ctrl+C cancels the command; rollback depends on its transaction boundaries.
            print(CLI_INTERRUPTED_MESSAGE, file=sys.stderr)
            exit_code = CLI_INTERRUPTED_EXIT_CODE
        raise SystemExit(exit_code)
