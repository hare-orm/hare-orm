from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.core.config.config_secrets import ConfigSecrets
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class DBUrlConfig:
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ConfigurationError("DBUrlConfig.url must be a non-empty string")

    def __repr__(self) -> str:
        """Shows the URL with its password and secret query parameters masked."""
        return f"{type(self).__name__}(url={ConfigSecrets.mask_secrets(self.url)!r})"

    def to_config(self) -> str:
        return self.url
