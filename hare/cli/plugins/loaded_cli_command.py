from __future__ import annotations

from dataclasses import dataclass

from hare.cli.plugins.cli_command import CLICommand


@dataclass(frozen=True)
class LoadedCLICommand:
    """A plugin command together with where it came from, for help text and warnings."""

    command: CLICommand
    source: str
