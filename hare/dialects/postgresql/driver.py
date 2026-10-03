from __future__ import annotations

from typing import Any

from hare.dialects.base.constants import FALSE_STRINGS, TRUE_STRINGS
from hare.dialects.base.driver import Driver
from hare.dialects.postgresql.constants import (
    POSTGRES_DEFAULT_PORT,
    POSTGRES_SSL_MODES,
    POSTGRES_SSL_QUERY_PARAMS,
    POSTGRESQL_DIALECT,
    POSTGRESQL_RETRYABLE_SQLSTATES,
    REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL,
)
from hare.exceptions import ConfigurationError


class PostgresqlDriver(Driver):
    """What every PostgreSQL driver shares: the DB_URL credentials, TLS modes and test databases."""

    dialect = POSTGRESQL_DIALECT
    authority_credentials = {
        "hostname": "host",
        "port": "port",
        "username": "user",
        "password": "password",  # nosec B105 - the name of a credential, not its value
    }
    default_credentials = {"port": POSTGRES_DEFAULT_PORT}

    def get_query_credentials(self, query_values: dict[str, str]) -> dict[str, Any]:
        ssl_query_params = [key for key in POSTGRES_SSL_QUERY_PARAMS if key in query_values]
        if len(ssl_query_params) > 1:
            raise ConfigurationError(f"DB_URL sets the SSL mode more than once: {ssl_query_params}")
        credentials: dict[str, Any] = {}
        for ssl_query_param in ssl_query_params:
            mode = self.get_ssl_mode(ssl_query_param, query_values.pop(ssl_query_param))
            credentials.update(self.get_ssl_credentials(ssl_query_param, mode))
        credentials.update(super().get_query_credentials(query_values))
        return credentials

    def is_retryable(self, error: BaseException) -> bool:
        # Both drivers carry the server's SQLSTATE on the exception: 40001 serialization_failure
        # and 40P01 deadlock_detected abort the transaction for a concurrent one.
        return getattr(error, "sqlstate", None) in POSTGRESQL_RETRYABLE_SQLSTATES

    def get_known_query_parameters(self) -> list[str]:
        return list(dict.fromkeys([*self.connection_options.names, *POSTGRES_SSL_QUERY_PARAMS]))

    @staticmethod
    def get_ssl_mode(query_param: str, raw_value: str) -> str | bool:
        """The TLS mode an SSL query parameter names.

        Args:
            query_param: One of POSTGRES_SSL_QUERY_PARAMS.
            raw_value: The parameter's value.

        Returns:
            A libpq ``sslmode`` value, or a boolean for ``ssl=true``/``ssl=false``.

        Raises:
            ConfigurationError: If the value is neither.
        """
        value = raw_value.strip().lower()
        if value in POSTGRES_SSL_MODES:
            return value
        if query_param == "ssl" and value in TRUE_STRINGS:
            return True
        if query_param == "ssl" and value in FALSE_STRINGS:
            return False
        expected = f"one of {sorted(POSTGRES_SSL_MODES)}" + (" or a boolean" if query_param == "ssl" else "")
        raise ConfigurationError(
            f"Invalid value {raw_value!r} for DB_URL query parameter {query_param!r}: expected {expected}"
        )

    def get_ssl_credentials(self, query_param: str, mode: str | bool) -> dict[str, Any]:
        """The driver's credentials for a TLS mode.

        Args:
            query_param: The DB_URL query parameter that gave the mode.
            mode: The mode.

        Returns:
            The credentials.

        Raises:
            UnSupportedError: If the driver doesn't support the mode.
        """
        raise NotImplementedError

    def get_testing_path(self, path: str, reuse_databases: bool) -> tuple[str, dict[str, Any]]:
        if not reuse_databases or "{}" not in path:
            return super().get_testing_path(path, reuse_databases)
        from hare.contrib.test.reusable_databases import ReusableTestDatabases

        path, lease_number = ReusableTestDatabases.acquire(path)
        return path, {REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL: lease_number}
