from __future__ import annotations

import re
import urllib.parse as urlparse
from collections.abc import Iterable, Mapping
from types import ModuleType
from typing import Any

from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.dialects.base.connection.constants import DB_URL_AUTHORITY_HOST_PATTERN
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError


class DbUrlConfigGenerator:
    """Parses a DB_URL string into the hare-style connections/apps config dict."""

    authority_host_regex = re.compile(DB_URL_AUTHORITY_HOST_PATTERN)

    @staticmethod
    def _quote_userinfo_component(component: str) -> str:
        """Escapes the characters of a username or password that break ``urlparse``: ``[``/``]``,
        ``#``, ``?`` and ``/``. Everything else, ``%`` included, stays as it is.
        """
        return (
            component.replace("[", "%5B")
            .replace("]", "%5D")
            .replace("#", "%23")
            .replace("?", "%3F")
            .replace("/", "%2F")
        )

    @staticmethod
    def _quote_url_userinfo(db_url: str) -> str:
        """Encode characters in the userinfo section that break urlparse - see
        ``_quote_userinfo_component``'s own docstring for exactly which ones and why.
        """
        scheme_end = db_url.find("://")
        if scheme_end == -1:
            return db_url

        scheme = db_url[: scheme_end + 3]
        rest = db_url[scheme_end + 3 :]

        at_position = DbUrlConfigGenerator.userinfo_end_position(rest)
        if at_position == -1:
            return db_url

        userinfo = rest[:at_position]
        after_userinfo = rest[at_position:]

        colon_position = userinfo.find(":")
        if colon_position == -1:
            username = DbUrlConfigGenerator._quote_userinfo_component(userinfo)
            return scheme + username + after_userinfo

        username = userinfo[:colon_position]
        password = userinfo[colon_position + 1 :]
        username_quoted = DbUrlConfigGenerator._quote_userinfo_component(username)
        password_quoted = DbUrlConfigGenerator._quote_userinfo_component(password)
        return scheme + username_quoted + ":" + password_quoted + after_userinfo

    @staticmethod
    def userinfo_end_position(url_rest: str) -> int:
        """Finds the "@" separating userinfo from the host in a DB_URL without its scheme.

        The userinfo may itself contain literal "@", "/", "?" and "#", and the query may contain
        "@" - so the separator is the first "@" followed by a well-formed host[:port] whose
        remaining path holds no further "@" outside the query.

        Args:
            url_rest: The DB_URL after "scheme://".

        Returns:
            The separator's index, or -1 when the URL has no userinfo.
        """
        at_positions = [position for position, character in enumerate(url_rest) if character == "@"]
        for at_position in at_positions:
            host_match = DbUrlConfigGenerator.authority_host_regex.match(url_rest, at_position + 1)
            if host_match is None:
                continue
            path_and_query = url_rest[host_match.end() :]
            path = path_and_query.split("?", 1)[0].split("#", 1)[0]
            if "@" not in path:
                return at_position
        return at_positions[-1] if at_positions else -1

    @staticmethod
    def _get_decoded_host(url: urlparse.ParseResult) -> str | None:
        """The DB_URL's host, percent-decoded - a unix-socket directory (``%2Fvar%2Frun``) or an
        IPv6 zone id (``[fe80::1%25eth0]``) is only valid in a URL once encoded.

        Args:
            url: The parsed DB_URL.

        Returns:
            The decoded host, or None when the URL has none.
        """
        host_and_port = url.netloc.rpartition("@")[2]
        if host_and_port.startswith("["):
            host = host_and_port[1 : host_and_port.find("]")]
        else:
            host = host_and_port.partition(":")[0]
        return urlparse.unquote(host) or None

    @staticmethod
    def expand(db_url: str, testing: bool = False, reuse_databases: bool = False) -> dict[str, Any]:
        """Parses a DB_URL into a connection config.

        Args:
            db_url: The database URL.
            testing: Whether a "{}" placeholder in the database name is filled with a fresh name.
            reuse_databases: With ``testing``, fill the placeholder with a reusable test database
                when the driver has them.

        Returns:
            The ``{"engine": ..., "credentials": ...}`` connection config.

        Raises:
            ConfigurationError: If the URL is malformed or names an unknown scheme or parameter.
        """
        driver = DialectRegistry.get_driver_for_url_scheme(db_url.partition("://")[0])
        if driver.url_has_userinfo:
            db_url = DbUrlConfigGenerator._quote_url_userinfo(db_url)
        url = urlparse.urlparse(db_url)
        path = driver.get_url_path(url)

        credentials: dict[str, Any] = dict(driver.default_credentials)
        query_values = {key: values[-1] for key, values in urlparse.parse_qs(url.query).items()}
        authority = driver.authority_credentials
        # A host/user/password given only as a query parameter (e.g. a unix socket directory in
        # ?host=/var/run/postgresql) is kept rather than overwritten with None.
        host = DbUrlConfigGenerator._get_decoded_host(url) if authority.get("hostname") else None
        try:
            url_port = url.port if authority.get("port") else None
        except ValueError as error:
            raise ConfigurationError(f"Invalid port in DB_URL: {error}") from None
        authority_values = {"hostname": host, "port": url_port, "username": url.username, "password": url.password}
        for component, authority_value in authority_values.items():
            credential_name = authority.get(component)
            if credential_name and authority_value not in {None, ""} and credential_name in query_values:
                raise ConfigurationError(
                    f"DB_URL sets {credential_name!r} twice - both in the address and as a query parameter"
                )
        credentials.update(driver.get_query_credentials(dict(query_values)))

        if testing and path:
            path = path.replace("\\{", "{").replace("\\}", "}")
            path, testing_credentials = driver.get_testing_path(path, reuse_databases)
            credentials.update(testing_credentials)
        credentials[driver.path_credential] = path

        if authority.get("hostname") and (host or authority["hostname"] not in query_values):
            credentials[authority["hostname"]] = host
        if authority.get("port") and url_port is not None:
            credentials[authority["port"]] = url_port
        for component, url_value in (("username", url.username), ("password", url.password)):
            credential_name = authority.get(component)
            if credential_name and (url_value or credential_name not in query_values):
                # None, not "", lets the driver read the user and password from the environment.
                credentials[credential_name] = urlparse.unquote(url_value) if url_value else None
        return {"engine": driver.name, "credentials": credentials}

    @staticmethod
    def build(
        db_url: str,
        app_modules: Mapping[str, Iterable[str | ModuleType]],
        connection_label: str | None = None,
        testing: bool = False,
        reuse_databases: bool = False,
    ) -> dict[str, Any]:
        _connection_label = connection_label or DEFAULT_CONNECTION_NAME
        return {
            "connections": {_connection_label: DbUrlConfigGenerator.expand(db_url, testing, reuse_databases)},
            "apps": {
                app_label: {"models": modules, "default_connection": _connection_label}
                for app_label, modules in app_modules.items()
            },
        }
