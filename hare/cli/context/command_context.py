from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Generator, Iterable
from typing import Any

from hare.cli.constants import MIGRATION_GRAPH_ERRORS
from hare.cli.context.cli_context import CLIContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.core.config import AppConfig, HareConfig
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.hare_context import HareContext
from hare.exceptions import (
    ConfigurationError,
    DatabaseError,
    DBConnectionError,
)
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.exceptions import UnknownMigrationError


class CommandContext:
    """What every ``hare`` command needs: loading the config, selecting apps, and the context managers
    turning a bad config or connection into a ``CLIError``.
    """

    @staticmethod
    @contextlib.asynccontextmanager
    async def hare_cli_context(
        config: dict[str, Any] | HareConfig, *, connect: bool = True
    ) -> AsyncGenerator[HareContext]:
        """A Hare context initialized with the configuration, for the block.

        Args:
            config: The configuration.
            connect: Whether the connections are opened - a command reading files alone opens none.

        Yields:
            The context.

        Raises:
            CLIError: The configuration or a model module is wrong, or the database can't be reached.
        """
        async with HareContext() as context:
            try:
                await context.init(config=config, connect=connect)
            except (ConfigurationError, DBConnectionError) as error:
                # A bad app config or a model module failing to import - Apps reports both as
                # ConfigurationError.
                raise CLIError(str(error)) from None
            yield context

    @staticmethod
    @contextlib.asynccontextmanager
    async def database_error_boundary() -> AsyncGenerator[None]:
        """Turns a database connectivity failure into a clean CLIError - shared by every command
        that runs a real query against the configured connection (migrate,
        history, inspectdb), so the raw driver exception never reaches the user as a traceback."""
        try:
            yield
        except DBConnectionError as error:
            raise CLIError(f"Could not connect to the database: {error}") from None
        except ConfigurationError as error:
            # A connection made after init can fail its own configuration check; the message is
            # already a complete sentence, and many ConfigurationErrors have nothing to do with
            # connecting.
            raise CLIError(str(error)) from None
        except DatabaseError as error:
            # The connection works but the SQL failed - not "could not connect".
            raise CLIError(f"Database operation failed: {error}") from None

    @staticmethod
    @contextlib.contextmanager
    def migration_graph_error_boundary(*, unknown_is_usage: bool = False) -> Generator[None]:
        """Report migration graph failures, optionally treating an unknown target as a usage error."""
        try:
            yield
        except MIGRATION_GRAPH_ERRORS as error:
            if unknown_is_usage and isinstance(error, UnknownMigrationError):
                raise CLIUsageError(str(error)) from None
            raise CLIError(str(error)) from None

    @staticmethod
    def load_config(cli_context: CLIContext) -> HareConfig:
        """The invocation's configuration, shared by discovery and command execution.

        The reference comes from its option, the environment or ``pyproject.toml``. A loaded
        config is retained on the immutable CLI context so one-pass model declarations are
        consumed once, without a process-wide cache or changes to the original config mapping.

        Returns:
            HareConfig: Validated configuration object
        """
        return cli_context._loaded_config

    @staticmethod
    def get_connection_alias(config: HareConfig, connection_alias: str | None) -> str:
        """Select an explicit nonempty alias, otherwise the first configured connection.

        Args:
            config: The loaded command configuration.
            connection_alias: The requested alias, or None/empty for the first connection.

        Returns:
            The configured alias, preserving configuration insertion order for the default.

        Raises:
            CLIUsageError: A requested nonempty alias is not configured.
        """
        if connection_alias:
            CommandContext.check_connection_alias(config, connection_alias)
            return connection_alias
        return next(iter(config.connections))

    @staticmethod
    def check_connection_alias(config: HareConfig, connection_alias: str) -> None:
        """Require an explicitly selected connection to exist in the loaded configuration."""
        if connection_alias not in config.connections:
            raise CLIUsageError(f"Unknown connection alias: {connection_alias}")

    @staticmethod
    def get_app_config(config: HareConfig, app_label: str) -> AppConfig:
        """Return the configured app or report an unknown CLI label."""
        try:
            return config.apps[app_label]
        except KeyError:
            raise CLIUsageError(f"Unknown app label {app_label}") from None

    @staticmethod
    def check_app_labels(config: HareConfig, app_labels: Iterable[str]) -> None:
        """Refuse the first unknown app label in command-line order."""
        for app_label in app_labels:
            CommandContext.get_app_config(config, app_label)

    @staticmethod
    def select_apps(config: HareConfig, app_labels: Iterable[str] | None) -> dict[str, AppConfig]:
        """Select specific apps from config, or all if no labels specified."""
        if not config.apps:
            raise CLIError("No apps configured in HARE_ORM")
        if app_labels is None:
            return dict(config.apps)
        selected: dict[str, AppConfig] = {}
        for label in app_labels:
            selected[label] = CommandContext.get_app_config(config, label)
        return selected or dict(config.apps)

    @staticmethod
    def group_apps_by_connection(
        apps_config: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        return MigrationRequestParser.group_selected_apps_by_connection(
            configured_apps=apps_config,
            selected_apps=tuple(apps_config),
            default_connection_name=DEFAULT_CONNECTION_NAME,
        )
