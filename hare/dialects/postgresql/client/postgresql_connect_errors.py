from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.client.constants import (
    POSTGRESQL_AUTHENTICATION_FAILED_MESSAGE,
    POSTGRESQL_CONNECTION_FAILED_MESSAGE,
    POSTGRESQL_DEFAULT_DATABASE_CONNECTION_FAILED_MESSAGE,
    POSTGRESQL_INVALID_CONNECTION_PARAMETER_MESSAGE,
)
from hare.exceptions import ConfigurationError, DBConnectionError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient


class PostgresqlConnectErrors:
    """The hare exception of a failure to connect to PostgreSQL - the same for every driver and for
    a pool and a LISTEN connection alike. The driver only tells its own exceptions apart
    (``is_authentication_failure()``, ``is_missing_database_error()``,
    ``is_invalid_parameter_error()``, ``RETRYABLE_CONNECT_EXCEPTIONS``)."""

    @staticmethod
    def get_error(client: PostgresqlClient, error: BaseException, with_db: bool) -> Exception | None:
        """The hare exception of a driver's failure to connect.

        Args:
            client: The client.
            error: The driver's exception.
            with_db: Whether the configured database was connected to - False for the server's default.

        Returns:
            ``ConfigurationError`` for what no retry fixes (a refused credential, a missing database,
            an invalid connection parameter), ``DBConnectionError`` for any other failure to connect,
            None for an exception that isn't a failure to connect.
        """
        database = client.database if with_db else "default"
        if client.is_authentication_failure(error):
            return ConfigurationError(
                POSTGRESQL_AUTHENTICATION_FAILED_MESSAGE.format(database=database, user=client.user, error=error)
            )
        if client.is_missing_database_error(error):
            return ConfigurationError(PostgresqlConnectErrors.get_failure_message(client, error, with_db))
        if client.is_invalid_parameter_error(error):
            return ConfigurationError(
                POSTGRESQL_INVALID_CONNECTION_PARAMETER_MESSAGE.format(database=database, error=error)
            )
        if isinstance(error, client.RETRYABLE_CONNECT_EXCEPTIONS):
            return DBConnectionError(PostgresqlConnectErrors.get_failure_message(client, error, with_db))
        return None

    @staticmethod
    def get_failure_message(client: PostgresqlClient, error: BaseException, with_db: bool) -> str:
        """The text of a failure to connect, with the driver's own reason.

        Args:
            client: The client.
            error: The driver's exception.
            with_db: Whether the configured database was connected to.

        Returns:
            The text.
        """
        if with_db:
            return POSTGRESQL_CONNECTION_FAILED_MESSAGE.format(database=client.database, error=error)
        return POSTGRESQL_DEFAULT_DATABASE_CONNECTION_FAILED_MESSAGE.format(error=error)
