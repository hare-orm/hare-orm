from hare.exceptions import ConfigurationError, QueryError
from hare.migrations.exceptions import HareMigrationError

MIGRATION_GRAPH_ERRORS = (HareMigrationError, ConfigurationError, QueryError)
"""Failures raised while loading the migration files, building their dependency graph and replaying them."""

CLI_COMMAND_ENTRY_POINT_GROUP = "hare.cli"
"""Entry-point group installed packages register extra `hare` subcommands under."""

CLI_COMMAND_REFERENCE_SEPARATOR = ":"
"""Separates the module path from the class name in a command reference, "package.module:ClassName"."""

#: The Windows console API's standard output handle id.
STD_OUTPUT_HANDLE = -11

#: The Windows console mode flag that makes it render ANSI escape sequences.
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
