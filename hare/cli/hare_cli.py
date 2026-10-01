import argparse
import asyncio
import sys

from hare import Hare, __version__  # noqa: F401 - Hare re-exported for `cli_module.Hare` test monkeypatches
from hare.cli.arg_parser_builder import ArgParserBuilder
from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.cli.shell import ShellLauncher
from hare.exceptions import HareError


class HareCLI:
    """The ``hare`` command's entry point - argument parsing, the shell and the commands are classes of
    their own.
    """

    @staticmethod
    async def shell(ctx: CLIContext) -> None:
        """Launches IPython with the configured Hare context and every model in its namespace.

        Raises:
            CLIError: If IPython isn't installed.
        """
        if ShellLauncher.interactive_shell_class is None:
            raise CLIError("hare shell needs IPython: pip install hare-orm[ipython]")
        config = CommandContext.load_config(ctx)
        async with CommandContext.hare_cli_context(config) as hare_ctx:
            ShellLauncher.launch_ipython(ShellLauncher.build_namespace(hare_ctx))

    @staticmethod
    async def _run_shell(ctx: CLIContext, _args: argparse.Namespace) -> None:
        await HareCLI.shell(ctx)

    @staticmethod
    async def run_cli_async(argv: list[str] | None = None) -> int:
        global_options = ArgParserBuilder.parse_global_options(argv)
        plugin_commands = CLICommandRegistry.discover(CLIContext(config=global_options.config))
        parser = ArgParserBuilder.build_parser(plugin_commands)
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1

        ctx = CLIContext(config=args.config)
        try:
            result = await args.func(ctx, args)
        except CLIUsageError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except CLIError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except HareError as exc:
            # A hare error a command (a plugin's own run(), most often) let through - its message
            # is the useful part, not a traceback.
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return result if isinstance(result, int) else 0

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
    def main() -> None:
        HareCLI.configure_output_encoding()
        if sys.path[0] != ".":
            sys.path.insert(0, ".")
        raise SystemExit(asyncio.run(HareCLI.run_cli_async()))
