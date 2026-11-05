from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import ModuleType
from typing import Any

from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.dialects.base.connection.db_url_parts import DbUrlParts
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError


class DbUrlConfigGenerator:
    """Parses a DB_URL string into the hare-style connections/apps config dict."""

    @staticmethod
    def expand(db_url: str) -> dict[str, Any]:
        """Parses a DB_URL into a connection config.

        Args:
            db_url: The database URL.

        Returns:
            The ``{"engine": ..., "credentials": ...}`` connection config.

        Raises:
            ConfigurationError: If the URL is malformed or names an unknown scheme or parameter.
        """
        scheme, separator, _location = db_url.partition("://")
        if not separator:
            # Not shown: without its scheme the URL may begin with a user and a password.
            raise ConfigurationError(
                f"A DB_URL begins with its scheme - one of {DialectRegistry.get_url_schemes()} - got none"
            )
        driver = DialectRegistry.get_driver_for_url_scheme(scheme)
        parts = DbUrlParts.parse(db_url, has_userinfo=driver.url_has_userinfo)
        path = driver.get_url_path(parts.parsed)
        credentials: dict[str, Any] = dict(driver.default_credentials)
        query_values = dict(parts.query)
        authority = driver.authority_credentials
        # A host/user/password given only as a query parameter (e.g. a unix socket directory in
        # ?host=/var/run/db) is kept rather than overwritten with None.
        host = parts.host if authority.get("hostname") else None
        url_port = parts.port if authority.get("port") else None
        authority_values = {"hostname": host, "port": url_port, "username": parts.username, "password": parts.password}
        for component, authority_value in authority_values.items():
            credential_name = authority.get(component)
            if credential_name and authority_value is not None and credential_name in query_values:
                raise ConfigurationError(
                    f"DB_URL sets {credential_name!r} twice - both in the address and as a query parameter"
                )
        credentials.update(driver.get_query_credentials(dict(query_values)))

        credentials[driver.path_credential] = path

        if authority.get("hostname") and (host or authority["hostname"] not in query_values):
            credentials[authority["hostname"]] = host
        if authority.get("port") and url_port is not None:
            credentials[authority["port"]] = url_port
        for component, url_value in (("username", parts.username), ("password", parts.password)):
            credential_name = authority.get(component)
            if credential_name and (url_value is not None or credential_name not in query_values):
                credentials[credential_name] = url_value
        return {"engine": driver.name, "credentials": credentials}

    @staticmethod
    def build(
        db_url: str,
        app_modules: Mapping[str, Iterable[str | ModuleType]],
        connection_label: str | None = None,
    ) -> dict[str, Any]:
        """The configuration mapping of one connection from its database URL, every app on it.

        Args:
            db_url: The database URL.
            app_modules: The app labels, each to the modules of its models.
            connection_label: The connection's alias - ``default`` when None.

        Returns:
            The mapping.
        """
        _connection_label = connection_label or DEFAULT_CONNECTION_NAME
        return {
            "connections": {_connection_label: DbUrlConfigGenerator.expand(db_url)},
            "apps": {
                app_label: {"models": modules, "default_connection": _connection_label}
                for app_label, modules in app_modules.items()
            },
        }
