from __future__ import annotations

from dataclasses import dataclass

from hare.core.config.config_secrets import ConfigSecrets


@dataclass(frozen=True)
class DBUrlConfig:
    """A connection of the configuration given by its database URL alone."""

    url: str

    def __repr__(self) -> str:
        """Shows the URL with its password and secret query parameters masked."""
        return f"{type(self).__name__}(url={ConfigSecrets.mask_secrets(self.url)!r})"

    def to_config(self) -> str:
        return self.url
