from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare.core.config.constants import PASSWORD_LOG_MASK, SECRET_CONFIG_KEY_MARKERS
from hare.dialects.base.connection.db_url_parts import DbUrlParts
from hare.exceptions import ConfigurationError


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
        # Deferred: driver registration itself reads configuration.
        from hare.dialects.dialect_registry import DialectRegistry

        scheme = db_url[:scheme_end]
        driver = DialectRegistry.find_driver_for_url_scheme(scheme)
        try:
            parts = DbUrlParts.parse(db_url, has_userinfo=driver is None or driver.url_has_userinfo)
        except ConfigurationError:
            # A malformed URL has no trustworthy component boundary. Hide its whole location.
            return f"{scheme}://{PASSWORD_LOG_MASK}"
        return parts.masked(ConfigSecrets.is_secret_key, PASSWORD_LOG_MASK)
