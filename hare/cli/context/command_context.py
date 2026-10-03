import contextlib
from collections.abc import AsyncGenerator, Iterable
from typing import Any

from hare.cli.config_locator import ConfigLocator
from hare.cli.context.cli_context import CLIContext
from hare.cli.exceptions import CLIError, CLIUsageError
from hare.core.config import AppConfig, HareConfig
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.context import HareContext
from hare.exceptions import (
    ConfigurationError,
    DatabaseError,
    DBConnectionError,
)


class CommandContext:
    """What every ``hare`` command needs: loading the config, selecting apps, and the context managers
    turning a bad config or connection into a ``CLIError``.
    """

    @staticmethod
    @contextlib.asynccontextmanager
    async def hare_cli_context(
        config: dict[str, Any] | HareConfig,
    ) -> AsyncGenerator[HareContext]:
        async with HareContext() as ctx:
            try:
                await ctx.init(config=config)
            except (ConfigurationError, DBConnectionError) as exc:
                # A bad app config or a model module failing to import - Apps reports both as
                # ConfigurationError.
                raise CLIError(str(exc)) from None
            yield ctx

    @staticmethod
    @contextlib.asynccontextmanager
    async def database_error_boundary() -> AsyncGenerator[None]:
        """Turns a database connectivity failure into a clean CLIError - shared by every command
        that runs a real query against the configured connection (migrate,
        history, inspectdb), so the raw driver exception never reaches the user as a traceback."""
        try:
            yield
        except DBConnectionError as exc:
            raise CLIError(f"Could not connect to the database: {exc}") from None
        except ConfigurationError as exc:
            # A connection made after init can fail its own configuration check; the message is
            # already a complete sentence, and many ConfigurationErrors have nothing to do with
            # connecting.
            raise CLIError(str(exc)) from None
        except DatabaseError as exc:
            # The connection works but the SQL failed - not "could not connect".
            raise CLIError(f"Database operation failed: {exc}") from None

    @staticmethod
    def load_config(ctx: CLIContext) -> HareConfig:
        """Load Hare ORM configuration from various sources.

        Returns:
            HareConfig: Validated configuration object
        """
        config_value = ctx.config or ConfigLocator.locate()
        if not config_value:
            raise CLIUsageError(
                "You must specify HARE_ORM in option or env, or pyproject.toml [tool.hare]",
            )
        try:
            return HareConfig.load(config_value)
        except ConfigurationError as exc:
            raise CLIError(f"Invalid Hare ORM configuration {config_value!r}: {exc}") from None

    @staticmethod
    def select_apps(config: HareConfig, app_labels: Iterable[str] | None) -> dict[str, AppConfig]:
        """Select specific apps from config, or all if no labels specified."""
        if not config.apps:
            raise CLIError("No apps configured in HARE_ORM")
        if not app_labels:
            return dict(config.apps)
        selected: dict[str, AppConfig] = {}
        for label in app_labels:
            if label not in config.apps:
                raise CLIUsageError(f"Unknown app label {label}")
            selected[label] = config.apps[label]
        return selected

    @staticmethod
    def group_apps_by_connection(
        apps_config: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        apps_by_connection: dict[str, dict[str, dict[str, Any]]] = {}
        for label, app_config in apps_config.items():
            connection_name = app_config.get("default_connection", DEFAULT_CONNECTION_NAME)
            apps_by_connection.setdefault(connection_name, {})[label] = app_config
        return apps_by_connection
