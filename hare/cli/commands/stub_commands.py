"""The `hare` CLI's stubs command: stubs of the modules of models for pyright and Pylance."""

from __future__ import annotations

import argparse
from pathlib import Path

from hare.cli.context.cli_context import CLIContext
from hare.cli.context.command_context import CommandContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.stubs.constants import MAX_RELATION_DEPTH
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels


class StubCommands:
    """Implements stubs - each module of models as pyright reads it, its querysets taking the keys
    and values of its models (``StubWriter``)."""

    @staticmethod
    async def stubs(cli_context: CLIContext, output: str, relation_depth: int, check: bool) -> int | None:
        """Generate stubs from models bound without opening database connections.

        Args:
            cli_context: The command's configuration and import context.
            output: The directory for the generated model modules.
            relation_depth: The maximum number of relations crossed by generated filter keys.
            check: Compare the expected stubs with existing files without writing them. Files for
                modules absent from the current configuration are not checked or removed.

        Returns:
            1 when ``check`` finds a missing or outdated stub, None otherwise.

        Raises:
            CLIUsageError: ``relation_depth`` is out of its range, or ``output`` holds a NUL
                character, which no path holds.
            CLIError: A generated stub can't be read or written at its output path.
        """
        if not 0 <= relation_depth <= MAX_RELATION_DEPTH:
            raise CLIUsageError(f"--relation-depth must be 0 to {MAX_RELATION_DEPTH}, got {relation_depth}")
        if "\0" in output:
            raise CLIUsageError(f"--output must be a path, got {output!r}")
        writer = StubWriter(BoundModels(CommandContext.load_config(cli_context)), Path(output), relation_depth)
        try:
            if check:
                outdated = writer.get_outdated()
                for path in outdated:
                    print(f"Outdated stub: {path.as_posix()}")
                return 1 if outdated else None
            written_paths = writer.write()
        # A ValueError is a path a stub's name can't go into - "//" is the root of no server on Windows.
        except (OSError, ValueError) as error:
            raise CLIError(f"Can't write the stubs into {output!r}: {error}") from error
        for path in written_paths:
            print(f"Wrote {path.as_posix()}")
        return None

    @staticmethod
    async def run_stubs(cli_context: CLIContext, args: argparse.Namespace) -> int | None:
        return await StubCommands.stubs(cli_context, args.output, args.relation_depth, args.check)
