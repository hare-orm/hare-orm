from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

from hare.core.config.constants import PASSWORD_LOG_MASK, SECRET_CONFIG_KEY_MARKERS
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator


class ConfigSecrets:
    """Masks the secrets of a configuration - passwords in DB URLs and credentials - for logs and
    reprs."""

    @staticmethod
    def get_masked_connections(connections_config: Mapping[str, Any]) -> str:
        """Renders a connections config for logging with every password masked.

        Args:
            connections_config: Connection name -> DB URL string or engine/credentials dict.

        Returns:
            The config's string form with passwords masked.
        """
        return str(ConfigSecrets.mask_secrets(connections_config))

    @staticmethod
    def mask_secrets(value: Any) -> Any:
        """Masks every secret inside one connection-config value, however deeply nested.

        Args:
            value: A DB URL, a credentials/connection mapping, or any other config value.

        Returns:
            A copy of ``value`` with secret-named entries and URL passwords masked.
        """
        if isinstance(value, str):
            return ConfigSecrets.mask_url_secrets(value) if "://" in value else value
        if isinstance(value, Mapping):
            masked_mapping: dict[Any, Any] = {}
            for key, item in value.items():
                if ConfigSecrets.is_secret_key(key) and item is not None:
                    masked_mapping[key] = PASSWORD_LOG_MASK
                else:
                    masked_mapping[key] = ConfigSecrets.mask_secrets(item)
            return masked_mapping
        if isinstance(value, list | tuple):
            return type(value)(ConfigSecrets.mask_secrets(item) for item in value)
        return value

    @staticmethod
    def is_secret_key(key: Any) -> bool:
        """Whether a config key names a secret (password, passwd, sslpassword, ...).

        Args:
            key: The mapping key or query parameter name.

        Returns:
            True when the key's value must be masked.
        """
        return isinstance(key, str) and any(marker in key.lower() for marker in SECRET_CONFIG_KEY_MARKERS)

    @staticmethod
    def mask_url_secrets(db_url: str) -> str:
        """Masks the password in a DB URL's userinfo and every secret query parameter.

        Args:
            db_url: The connection URL.

        Returns:
            The DB URL with its passwords masked.
        """
        scheme_end = db_url.find("://")
        if scheme_end == -1:
            return db_url
        url_rest = db_url[scheme_end + 3 :]
        # A driver whose URL takes no userinfo (a file path may hold an "@") has none to mask; an
        # unknown scheme is masked as if it had.
        # Deferred: the dialect registry loads the dialects, which read the configuration.
        from hare.dialects.dialect_registry import DialectRegistry

        driver = DialectRegistry.find_driver_for_url_scheme(db_url[:scheme_end])
        has_userinfo = driver is None or driver.url_has_userinfo
        at_position = DbUrlConfigGenerator.userinfo_end_position(url_rest) if has_userinfo else -1
        userinfo = url_rest[:at_position] if at_position != -1 else ""
        after_userinfo = url_rest[at_position:] if at_position != -1 else url_rest
        username, separator, raw_password = userinfo.partition(":")
        if separator:
            masked_password = PASSWORD_LOG_MASK if raw_password else ""
            userinfo = f"{username}:{masked_password}"
        location, query_separator, query_and_fragment = after_userinfo.partition("?")
        if query_separator:
            query, fragment_separator, fragment = query_and_fragment.partition("#")
            masked_parameters = []
            for parameter in query.split("&"):
                name, equals_sign, _ = parameter.partition("=")
                if equals_sign and ConfigSecrets.is_secret_key(urllib.parse.unquote_plus(name)):
                    parameter = f"{name}={PASSWORD_LOG_MASK}"
                masked_parameters.append(parameter)
            after_userinfo = f"{location}?{'&'.join(masked_parameters)}{fragment_separator}{fragment}"
        return f"{db_url[: scheme_end + 3]}{userinfo}{after_userinfo}"
