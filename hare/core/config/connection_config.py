from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hare.core.config.config_secrets import ConfigSecrets
from hare.core.config.config_section import ConfigSection
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class ConnectionConfig(ConfigSection):
    engine: str | None = None
    credentials: dict[str, Any] = field(default_factory=dict)
    db_url: str | None = None

    def __post_init__(self) -> None:
        if self.db_url is not None:
            if self.engine is not None or self.credentials:
                raise ConfigurationError("ConnectionConfig cannot set db_url together with engine/credentials")
            if not isinstance(self.db_url, str) or not self.db_url:
                raise ConfigurationError("ConnectionConfig.db_url must be a non-empty string")
            return

        if self.engine is None or not isinstance(self.engine, str) or not self.engine:
            raise ConfigurationError("ConnectionConfig.engine must be a non-empty string")
        if not isinstance(self.credentials, dict):
            raise ConfigurationError("ConnectionConfig.credentials must be a dict")

    def __repr__(self) -> str:
        """Shows the config with every secret credential and URL password masked."""
        return (
            f"{type(self).__name__}(engine={self.engine!r}, "
            f"credentials={ConfigSecrets.mask_secrets(self.credentials)!r}, "
            f"db_url={ConfigSecrets.mask_secrets(self.db_url)!r})"
        )

    def to_config(self) -> str | dict[str, Any]:
        if self.db_url is not None:
            # Wrapped in a dict - a bare string would read back as a DBUrlConfig.
            return {"db_url": self.db_url}
        return {"engine": self.engine, "credentials": self.credentials}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = "a connection") -> Self:
        """Builds the config from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages - ``HareConfig.from_dict()`` passes
                the connection's name.

        Raises:
            ConfigurationError: If ``data`` isn't a mapping, holds an unknown key, or a value is
                invalid.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError("ConnectionConfig must be created from a mapping")
        cls.check_known_keys(data, section)
        credentials = data.get("credentials", {})
        if not isinstance(credentials, Mapping):
            raise ConfigurationError("ConnectionConfig.credentials must be a dict")
        return cls(
            engine=data.get("engine"),
            credentials=dict(credentials),
            db_url=data.get("db_url"),
        )
