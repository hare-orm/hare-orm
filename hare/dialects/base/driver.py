from __future__ import annotations

import urllib.parse as urlparse
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.dialects.base.connection_options import ConnectionOptions
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect


class Driver:
    """How hare connects to a database: the client class, its credentials and its DB_URL schemes.

    Attributes:
        name: The name a connection config's ``engine`` refers to.
        dialect: The dialect of the databases the driver connects to.
        url_schemes: The DB_URL schemes that pick this driver.
        path_credential: The credential the DB_URL path fills (a database name or a file path).
        authority_credentials: The credentials the DB_URL host, port, username and password fill,
            keyed by ``hostname``/``port``/``username``/``password``; a missing key is ignored.
        default_credentials: Credentials a DB_URL leaves unset.
        connection_options: The settings the driver's connections take - a DB_URL query
            parameter of one of their names is checked and converted as that setting.
        strict_query_parameters: Whether a DB_URL query parameter must be one of
            ``connection_options``.
        url_has_userinfo: Whether a DB_URL may carry a username and password.
    """

    name: str
    dialect: Dialect
    url_schemes: tuple[str, ...] = ()
    path_credential: str = "database"
    authority_credentials: Mapping[str, str] = {}
    default_credentials: Mapping[str, Any] = {}
    connection_options: ConnectionOptions = ConnectionOptions()
    strict_query_parameters: bool = False
    url_has_userinfo: bool = True

    def get_client_class(self, credentials: dict[str, Any]) -> type[DatabaseClient]:
        """The client class for a connection.

        Args:
            credentials: The connection's credentials; a credential only the choice needs is
                removed from it.

        Returns:
            The client class.
        """
        raise NotImplementedError

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        """Every client class the driver's connections run on - each connection client and its
        transaction client - for instrumentation to wrap their query-executing methods.

        Returns:
            The classes; empty where the driver can't be loaded (its native extension isn't
            built).
        """
        raise NotImplementedError

    def is_retryable(self, error: BaseException) -> bool:
        """Returns whether ``error``, raised by the driver for a failed statement, means the
        database aborted it because of a concurrent transaction, so that running the whole
        transaction again can succeed - translated to ``TransactionRetryError``.

        Args:
            error: The driver's exception.

        Returns:
            Whether the transaction can be retried; False for a driver that tells no such errors apart.
        """
        return False

    def get_url_path(self, url: urlparse.ParseResult) -> str | None:
        """The ``path_credential`` value a DB_URL gives.

        Args:
            url: The parsed DB_URL.

        Returns:
            The path, or None when the DB_URL has none.

        Raises:
            ConfigurationError: If the driver needs a path and the DB_URL has none.
        """
        return urlparse.unquote(url.path[1:]) or None

    def get_testing_path(self, path: str, reuse_databases: bool) -> tuple[str, dict[str, Any]]:
        """The path a test run connects to, a ``{}`` placeholder filled with a fresh name.

        Args:
            path: The DB_URL path.
            reuse_databases: Whether a placeholder is filled with a reusable test database.

        Returns:
            The path and any credentials it adds.
        """
        return path.format(uuid.uuid4().hex), {}

    def get_query_credentials(self, query_values: dict[str, str]) -> dict[str, Any]:
        """The credentials a DB_URL's query parameters give.

        Args:
            query_values: The query parameters by name; the ones handled here are removed.

        Returns:
            The credentials.

        Raises:
            ConfigurationError: If a parameter is unknown or its value is invalid.
        """
        if self.strict_query_parameters:
            unknown_keys = sorted(key for key in query_values if key not in self.connection_options)
            if unknown_keys:
                raise ConfigurationError(
                    f"Unknown DB_URL query parameter {unknown_keys[0]!r} for the {self.name} driver: expected one "
                    f"of {sorted(self.get_known_query_parameters())}"
                )
        options = self.connection_options.by_name
        return {
            name: options[name].parse(raw_value) if name in options else raw_value
            for name, raw_value in query_values.items()
        }

    def get_known_query_parameters(self) -> list[str]:
        """The DB_URL query parameters the driver takes."""
        return self.connection_options.names

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
