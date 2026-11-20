from __future__ import annotations

import argparse

from hare import __version__
from hare.classes.value_descriptions import ValueDescriptions
from hare.cli.commands.database_shell_commands import DatabaseShellCommands
from hare.cli.commands.distributed_recovery_commands import DistributedRecoveryCommands
from hare.cli.commands.migration_commands import MigrationCommands
from hare.cli.commands.schema_commands import SchemaCommands
from hare.cli.commands.stub_commands import StubCommands
from hare.cli.parsed_command import CommandHandler
from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from hare.cli.plugins.loaded_cli_command import LoadedCLICommand
from hare.stubs.constants import DEFAULT_RELATION_DEPTH, DEFAULT_STUBS_DIRECTORY
from hare.transactions.constants import DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS


class ArgParserBuilder:
    """Builds the `hare` CLI's argparse.ArgumentParser: global options, every subcommand's
    arguments, and command handlers registered outside the parsed argument namespace."""

    @staticmethod
    def parse_name(raw_value: str) -> str:
        """Parses a name given on the command line - of an app, a migration, a connection, a schema.

        Args:
            raw_value: The raw command-line text.

        Returns:
            The name.

        Raises:
            argparse.ArgumentTypeError: The name is empty or blank - which would read as no name given.
        """
        if not raw_value.strip():
            raise argparse.ArgumentTypeError(f"takes a name, got {raw_value!r}")
        return raw_value

    @staticmethod
    def parse_older_than_seconds(raw_value: str) -> int:
        """Parses the `--older-than` argument.

        Args:
            raw_value: The raw command-line text.

        Returns:
            A nonnegative integer number of seconds.

        Raises:
            argparse.ArgumentTypeError: The value isn't an integer or is negative.
        """
        try:
            seconds = int(raw_value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{raw_value!r} is not an integer number of seconds") from None
        if seconds < 0:
            raise argparse.ArgumentTypeError(
                f"must be at least 0 seconds, got {ValueDescriptions.get_shown_text(seconds)}"
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
    def _add_init_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        init_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_init,
            "init",
            help="Create migrations packages for configured apps.",
        )
        init_parser.add_argument("app_labels", nargs="*")

    @staticmethod
    def _add_shell_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        # Imported here: the modules import each other.
        from hare.cli.hare_cli import HareCLI

        CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            HareCLI.run_shell,
            "shell",
            help="Start an IPython shell with the models loaded.",
        )

    @staticmethod
    def _add_makemigrations_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        makemigrations_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_makemigrations,
            "makemigrations",
            help="Create new migrations from model changes.",
        )
        makemigrations_parser.add_argument("app_labels", nargs="*")
        makemigrations_parser.add_argument("--empty", action="store_true", help="Create an empty migration.")
        makemigrations_parser.add_argument(
            "-n", "--name", type=ArgParserBuilder.parse_name, help="Use this name for the migration file."
        )
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
        makemigrations_parser.add_argument(
            "--rename",
            action="append",
            default=[],
            metavar="OLD=NEW",
            help=(
                "A rename to make instead of a removal and an addition: app.Model.old_field=new_field, "
                "app.OldModel=NewModel, or old_app.Model=new_app.Model for a model moved keeping its table."
            ),
        )
        makemigrations_parser.add_argument(
            "--no-input",
            action="store_true",
            help="Ask no rename question; keep automatic field matches and explicit --rename answers.",
        )

    @staticmethod
    def _add_squashmigrations_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        squashmigrations_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_squashmigrations,
            "squashmigrations",
            help="Squash a run of an app's migrations - [START] END, from its first one without START - into one.",
        )
        squashmigrations_parser.add_argument("app_label")
        squashmigrations_parser.add_argument(
            "migration_names",
            nargs="+",
            type=ArgParserBuilder.parse_name,
            metavar="MIGRATION",
            help="[START] END - the first and last migration squashed, by name or a unique prefix of it.",
        )
        squashmigrations_parser.add_argument(
            "--squashed-name",
            type=ArgParserBuilder.parse_name,
            help="The squashed migration's name after its number (squashed_<END> by default).",
        )

    @staticmethod
    def _add_migration_direction_arguments(parser: argparse.ArgumentParser) -> None:
        """The positional app_label/migration pair - ``zero`` unapplies every migration of the
        app - plus --fake/--dry-run."""
        parser.add_argument("app_label", nargs="?")
        parser.add_argument("migration", nargs="?", type=ArgParserBuilder.parse_name)
        parser.add_argument("--fake", action="store_true", help="Record migrations without executing SQL.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would run without changing DB state.")

    @staticmethod
    def _add_migrate_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        migrate_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_migrate,
            "migrate",
            help="Apply migrations, or unapply them back to an earlier one ('zero' for none).",
        )
        ArgParserBuilder._add_migration_direction_arguments(migrate_parser)
        migrate_parser.add_argument(
            "--lock-timeout",
            type=float,
            metavar="SECONDS",
            help="Fail a migration whose statement waits longer for a lock another session holds "
            '(the config\'s "migrations.lock_timeout" by default).',
        )

    @staticmethod
    def _add_history_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        history_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_history,
            "history",
            help="List applied migrations from the database.",
        )
        history_parser.add_argument("app_labels", nargs="*")

    @staticmethod
    def _add_heads_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        heads_parser = CLICommandRegistry.add_parser(
            subparsers, command_handlers, MigrationCommands.run_heads, "heads", help="List migration heads on disk."
        )
        heads_parser.add_argument("app_labels", nargs="*")

    @staticmethod
    def _add_inspectdb_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        inspectdb_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            SchemaCommands.run_inspectdb,
            "inspectdb",
            help="Generate hare-orm model source from an existing database schema.",
        )
        inspectdb_parser.add_argument("tables", nargs="*", help="Specific table names (default: every table).")
        inspectdb_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            type=ArgParserBuilder.parse_name,
            help="Connection alias to inspect (default: the first one configured).",
        )
        inspectdb_parser.add_argument(
            "--schema",
            default=None,
            type=ArgParserBuilder.parse_name,
            help="Schema to inspect - a schema, an attached database or a database of the server: the "
            "namespace of tables the database has (default: the connection's default schema).",
        )

    @staticmethod
    def _add_dbshell_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        dbshell_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            DatabaseShellCommands.run_dbshell,
            "dbshell",
            help="Run the database's own interactive client on a connection.",
        )
        dbshell_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            type=ArgParserBuilder.parse_name,
            help="Connection alias to open (default: the first one configured).",
        )

    @staticmethod
    def _add_stubs_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        stubs_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            StubCommands.run_stubs,
            "stubs",
            help="Write the stubs pyright and Pylance type-check the modules of models with.",
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

    @staticmethod
    def _add_checkmigrations_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        checkmigrations_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_checkmigrations,
            "checkmigrations",
            help="Check the migrations not applied yet for operations risky on the database in use; "
            "exits with 1 when one isn't listed in its migration's safety_exemptions.",
        )
        checkmigrations_parser.add_argument("app_labels", nargs="*")

    @staticmethod
    def _add_sqlmigrate_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        sqlmigrate_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            MigrationCommands.run_sqlmigrate,
            "sqlmigrate",
            help="Print the SQL for a migration.",
        )
        sqlmigrate_parser.add_argument("app_label", nargs="?", help="App label.")
        sqlmigrate_parser.add_argument(
            "migration_name", nargs="?", type=ArgParserBuilder.parse_name, help="Migration name."
        )
        sqlmigrate_parser.add_argument(
            "--backward",
            action="store_true",
            help="Generate SQL to unapply the migration.",
        )

    @staticmethod
    def _add_drift_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        drift_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            SchemaCommands.run_drift,
            "drift",
            help="Compare the live database schema against the current models; non-zero exit on mismatch.",
        )
        drift_parser.add_argument("app_labels", nargs="*")
        drift_parser.add_argument(
            "--connection",
            dest="connection_alias",
            default=None,
            type=ArgParserBuilder.parse_name,
            help="Connection alias to check (default: the first one configured).",
        )
        drift_parser.add_argument(
            "--schema",
            default=None,
            type=ArgParserBuilder.parse_name,
            help="Schema swept for untracked tables - a schema, an attached database or a database of the "
            "server: the namespace of tables the database has (default: the connection's current schema).",
        )
        drift_parser.add_argument(
            "--journal-table",
            dest="journal_tables",
            action="append",
            default=[],
            help="A migration journal besides hare_migrations - MigrationRecorder(table_name=...) - not reported "
            "as untracked. Repeatable.",
        )

    @staticmethod
    def _add_distributed_recover_parser(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser], command_handlers: dict[str, CommandHandler]
    ) -> None:
        distributed_recover_parser = CLICommandRegistry.add_parser(
            subparsers,
            command_handlers,
            DistributedRecoveryCommands.run_distributed_recover,
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
            help="Actually commit or roll back the prepared transactions, instead of only reporting.",
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

    @staticmethod
    def parse_global_options(argv: list[str] | None) -> argparse.Namespace:
        """Reads just `-c`/`--config` - needed to find the project's config (and the plugin
        commands it lists) before the full parser can be built.

        Args:
            argv: The command-line arguments, or None for ``sys.argv[1:]``.

        Returns:
            The leading config options and the untouched command tokens. Parsing stops at
            the subcommand, whose flags belong to its parser even when they start with ``-c``.
        """
        parser = argparse.ArgumentParser(prog="hare", add_help=False)
        parser.add_argument("-c", "--config")
        parser.add_argument("command_arguments", nargs=argparse.REMAINDER)
        global_options, _ = parser.parse_known_args(argv)
        return global_options

    @staticmethod
    def build_parser(
        plugin_commands: list[LoadedCLICommand] | None = None,
        *,
        command_handlers: dict[str, CommandHandler] | None = None,
    ) -> argparse.ArgumentParser:
        """Builds the `hare` parser: the built-in subcommands, then any plugin ones.

        Args:
            plugin_commands: Commands from installed packages and the project's config.
            command_handlers: Receives successful command registrations separately from
                argument destinations, which may use the same names as execution metadata.

        Returns:
            The parser.
        """
        parser = argparse.ArgumentParser(prog="hare")
        ArgParserBuilder._add_global_options(parser)
        subparsers = parser.add_subparsers(dest="command", required=True)
        if command_handlers is None:
            command_handlers = {}

        ArgParserBuilder._add_init_parser(subparsers, command_handlers)
        ArgParserBuilder._add_shell_parser(subparsers, command_handlers)
        ArgParserBuilder._add_makemigrations_parser(subparsers, command_handlers)
        ArgParserBuilder._add_squashmigrations_parser(subparsers, command_handlers)
        ArgParserBuilder._add_migrate_parser(subparsers, command_handlers)
        ArgParserBuilder._add_history_parser(subparsers, command_handlers)
        ArgParserBuilder._add_heads_parser(subparsers, command_handlers)
        ArgParserBuilder._add_inspectdb_parser(subparsers, command_handlers)
        ArgParserBuilder._add_dbshell_parser(subparsers, command_handlers)
        ArgParserBuilder._add_stubs_parser(subparsers, command_handlers)
        ArgParserBuilder._add_checkmigrations_parser(subparsers, command_handlers)
        ArgParserBuilder._add_sqlmigrate_parser(subparsers, command_handlers)
        ArgParserBuilder._add_drift_parser(subparsers, command_handlers)
        ArgParserBuilder._add_distributed_recover_parser(subparsers, command_handlers)
        CLICommandRegistry.register(subparsers, plugin_commands or [], command_handlers=command_handlers)

        return parser
