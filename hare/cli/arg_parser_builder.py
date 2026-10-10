from __future__ import annotations

import argparse

from hare import Hare, __version__  # noqa: F401 - Hare re-exported for `cli_module.Hare` test monkeypatches
from hare.cli.commands.database_shell_commands import DatabaseShellCommands
from hare.cli.commands.distributed_recovery_commands import DistributedRecoveryCommands
from hare.cli.commands.migration_commands import MigrationCommands
from hare.cli.commands.schema_commands import SchemaCommands
from hare.cli.commands.stub_commands import StubCommands
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.cli.plugins.loaded_cli_command import LoadedCLICommand
from hare.stubs.constants import DEFAULT_RELATION_DEPTH, DEFAULT_STUBS_DIRECTORY
from hare.transactions.constants import (
    DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS,
    MAX_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS,
)


class ArgParserBuilder:
    """Builds the `hare` CLI's argparse.ArgumentParser: global options, every subcommand's
    arguments, and the dispatch wiring (`set_defaults(func=...)`) pointing each subcommand at
    its own command class's `_run_*` adapter."""

    @staticmethod
    def parse_older_than_seconds(raw_value: str) -> int:
        """Parses the `--older-than` argument.

        Args:
            raw_value: The raw command-line text.

        Returns:
            The number of seconds, within 0..MAX_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS.

        Raises:
            argparse.ArgumentTypeError: If the value isn't an integer inside that range.
        """
        try:
            seconds = int(raw_value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{raw_value!r} is not an integer number of seconds") from None
        if not 0 <= seconds <= MAX_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS:
            raise argparse.ArgumentTypeError(
                f"must be between 0 and {MAX_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS} seconds, got {seconds}"
            )
        return seconds

    @staticmethod
    def _add_global_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-c",
            "--config",
            help="HareORM config: a module variable like settings.HARE_ORM, or a .json/.yml file",
        )
        parser.add_argument("-V", "--version", action="version", version=__version__)

    @staticmethod
    def _add_init_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        init_parser = subparsers.add_parser("init", help="Create migrations packages for configured apps.")
        init_parser.add_argument("app_labels", nargs="*")
        init_parser.set_defaults(func=MigrationCommands.run_init)

    @staticmethod
    def _add_shell_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        # Imported here: the modules import each other.
        from hare.cli.hare_cli import HareCLI

        shell_parser = subparsers.add_parser("shell", help="Start an IPython shell with the models loaded.")
        shell_parser.set_defaults(func=HareCLI.run_shell)

    @staticmethod
    def _add_makemigrations_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        makemigrations_parser = subparsers.add_parser(
            "makemigrations", help="Create new migrations from model changes."
        )
        makemigrations_parser.add_argument("app_labels", nargs="*")
        makemigrations_parser.add_argument("--empty", action="store_true", help="Create an empty migration.")
        makemigrations_parser.add_argument("-n", "--name", help="Use this name for the migration file.")
        makemigrations_parser.add_argument(
            "--check",
            action="store_true",
            help="Exit with a non-zero status if there are unmade migrations, without writing them.",
        )
        makemigrations_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what migrations would be made without actually writing them.",
        )
        makemigrations_parser.add_argument(
            "--merge",
            action="store_true",
            help="Merge conflicting migration heads for the given app(s) into one migration.",
        )
        makemigrations_parser.set_defaults(func=MigrationCommands.run_makemigrations)

    @staticmethod
    def _add_squashmigrations_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        squashmigrations_parser = subparsers.add_parser(
            "squashmigrations",
            help="Squash a run of an app's migrations - [START] END, from its first one without START - into one.",
        )
        squashmigrations_parser.add_argument("app_label")
        squashmigrations_parser.add_argument(
            "migration_names",
            nargs="+",
            metavar="MIGRATION",
            help="[START] END - the first and last migration squashed, by name or a unique prefix of it.",
        )
        squashmigrations_parser.add_argument(
            "--squashed-name", help="The squashed migration's name after its number (squashed_<END> by default)."
        )
        squashmigrations_parser.set_defaults(func=MigrationCommands.run_squashmigrations)

    @staticmethod
    def _add_migration_direction_arguments(parser: argparse.ArgumentParser) -> None:
        """The positional app_label/migration pair - ``zero`` unapplies every migration of the
        app - plus --fake/--dry-run."""
        parser.add_argument("app_label", nargs="?")
        parser.add_argument("migration", nargs="?")
        parser.add_argument("--fake", action="store_true", help="Record migrations without executing SQL.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would run without changing DB state.")

    @staticmethod
    def _add_migrate_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        migrate_parser = subparsers.add_parser(
            "migrate", help="Apply migrations, or unapply them back to an earlier one ('zero' for none)."
        )
        ArgParserBuilder._add_migration_direction_arguments(migrate_parser)
        migrate_parser.add_argument(
            "--lock-timeout",
            type=float,
            metavar="SECONDS",
            help="Fail a migration whose statement waits longer for a lock another session holds "
            '(the config\'s "migrations.lock_timeout" by default).',
        )
        migrate_parser.set_defaults(func=MigrationCommands.run_migrate_cmd)

    @staticmethod
    def _add_history_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        history_parser = subparsers.add_parser("history", help="List applied migrations from the database.")
        history_parser.add_argument("app_labels", nargs="*")
        history_parser.set_defaults(func=MigrationCommands.run_history)

    @staticmethod
    def _add_heads_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        heads_parser = subparsers.add_parser("heads", help="List migration heads on disk.")
        heads_parser.add_argument("app_labels", nargs="*")
        heads_parser.set_defaults(func=MigrationCommands.run_heads)

    @staticmethod
    def _add_inspectdb_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        inspectdb_parser = subparsers.add_parser(
            "inspectdb", help="Generate hare-orm model source from an existing database schema."
        )
        inspectdb_parser.add_argument("tables", nargs="*", help="Specific table names (default: every table).")
        inspectdb_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            help="Connection alias to inspect (default: the first one configured).",
        )
        inspectdb_parser.add_argument(
            "--schema",
            default=None,
            help="Schema to inspect (default: the connection's default schema). Ignored on a database "
            "without schemas.",
        )
        inspectdb_parser.set_defaults(func=SchemaCommands.run_inspectdb)

    @staticmethod
    def _add_dbshell_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        dbshell_parser = subparsers.add_parser(
            "dbshell", help="Run the database's own interactive client on a connection."
        )
        dbshell_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            help="Connection alias to open (default: the first one configured).",
        )
        dbshell_parser.set_defaults(func=DatabaseShellCommands.run_dbshell)

    @staticmethod
    def _add_stubs_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        stubs_parser = subparsers.add_parser(
            "stubs", help="Write the stubs pyright and Pylance type-check the modules of models with."
        )
        stubs_parser.add_argument(
            "--output", default=DEFAULT_STUBS_DIRECTORY, help="The directory of the stubs (default: typings)."
        )
        stubs_parser.add_argument(
            "--relation-depth",
            type=int,
            default=DEFAULT_RELATION_DEPTH,
            help="How many relations a filter key crosses at most (default: 2).",
        )
        stubs_parser.add_argument(
            "--check", action="store_true", help="Write nothing; exit 1 when a stub is missing or outdated."
        )
        stubs_parser.set_defaults(func=StubCommands.run_stubs)

    @staticmethod
    def _add_checkmigrations_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        checkmigrations_parser = subparsers.add_parser(
            "checkmigrations",
            help="Check the migrations not applied yet for operations risky on the database in use; "
            "exits with 1 when one isn't listed in its migration's safety_exemptions.",
        )
        checkmigrations_parser.add_argument("app_labels", nargs="*")
        checkmigrations_parser.set_defaults(func=MigrationCommands.run_checkmigrations)

    @staticmethod
    def _add_sqlmigrate_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        sqlmigrate_parser = subparsers.add_parser("sqlmigrate", help="Print the SQL for a migration.")
        sqlmigrate_parser.add_argument("app_label", nargs="?", help="App label.")
        sqlmigrate_parser.add_argument("migration_name", nargs="?", help="Migration name.")
        sqlmigrate_parser.add_argument(
            "--backward",
            action="store_true",
            help="Generate SQL to unapply the migration.",
        )
        sqlmigrate_parser.set_defaults(func=MigrationCommands.run_sqlmigrate)

    @staticmethod
    def _add_drift_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        drift_parser = subparsers.add_parser(
            "drift",
            help="Compare the live database schema against the current models; non-zero exit on mismatch.",
        )
        drift_parser.add_argument("app_labels", nargs="*")
        drift_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            help="Connection alias to check (default: the first one configured).",
        )
        drift_parser.add_argument(
            "--schema",
            default=None,
            help="Schema swept for untracked tables (default: the connection's current schema). Ignored "
            "on a database without schemas.",
        )
        drift_parser.set_defaults(func=SchemaCommands.run_drift)

    @staticmethod
    def _add_distributed_recover_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        distributed_recover_parser = subparsers.add_parser(
            "distributed-recover",
            help="Report (or finish) Transactions.distributed() prepared transactions left pending.",
        )
        distributed_recover_parser.add_argument(
            "--coordinator",
            required=True,
            help="Alias holding the hare_distributed_decisions decision log.",
        )
        distributed_recover_parser.add_argument(
            "--finish",
            action="store_true",
            help="Actually issue COMMIT PREPARED/ROLLBACK PREPARED, instead of only reporting.",
        )
        distributed_recover_parser.add_argument(
            "--older-than",
            dest="older_than_seconds",
            type=ArgParserBuilder.parse_older_than_seconds,
            default=DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS,
            help=(
                "Ignore anything younger than this many seconds - it may still be mid-flight "
                f"(default: {DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS})."
            ),
        )
        distributed_recover_parser.set_defaults(func=DistributedRecoveryCommands.run_distributed_recover)

    @staticmethod
    def parse_global_options(argv: list[str] | None) -> argparse.Namespace:
        """Reads just `-c`/`--config` - needed to find the project's config (and the plugin
        commands it lists) before the full parser can be built.

        Args:
            argv: The command-line arguments, or None for ``sys.argv[1:]``.

        Returns:
            The config options; everything else on the command line is left for the full parser.
        """
        parser = argparse.ArgumentParser(prog="hare", add_help=False)
        parser.add_argument("-c", "--config")
        global_options, _ = parser.parse_known_args(argv)
        return global_options

    @staticmethod
    def build_parser(plugin_commands: list[LoadedCLICommand] | None = None) -> argparse.ArgumentParser:
        """Builds the `hare` parser: the built-in subcommands, then any plugin ones.

        Args:
            plugin_commands: Commands from installed packages and the project's config.

        Returns:
            The parser.
        """
        parser = argparse.ArgumentParser(prog="hare")
        ArgParserBuilder._add_global_options(parser)
        subparsers = parser.add_subparsers(dest="command", required=True)

        ArgParserBuilder._add_init_parser(subparsers)
        ArgParserBuilder._add_shell_parser(subparsers)
        ArgParserBuilder._add_makemigrations_parser(subparsers)
        ArgParserBuilder._add_squashmigrations_parser(subparsers)
        ArgParserBuilder._add_migrate_parser(subparsers)
        ArgParserBuilder._add_history_parser(subparsers)
        ArgParserBuilder._add_heads_parser(subparsers)
        ArgParserBuilder._add_inspectdb_parser(subparsers)
        ArgParserBuilder._add_dbshell_parser(subparsers)
        ArgParserBuilder._add_stubs_parser(subparsers)
        ArgParserBuilder._add_checkmigrations_parser(subparsers)
        ArgParserBuilder._add_sqlmigrate_parser(subparsers)
        ArgParserBuilder._add_drift_parser(subparsers)
        ArgParserBuilder._add_distributed_recover_parser(subparsers)
        CLICommandRegistry.register(subparsers, plugin_commands or [])

        return parser
