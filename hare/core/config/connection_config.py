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
    """A connection of the configuration: an engine with its credentials, or a database URL."""

    engine: str | None = None
    credentials: dict[str, Any] = field(default_factory=dict)
    db_url: str | None = None

    def __post_init__(self) -> None:
        if self.db_url is not None and (self.engine is not None or self.credentials):
            raise ConfigurationError("ConnectionConfig cannot set db_url together with engine/credentials")

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
                the connection alias.

        Raises:
            ConfigurationError: If ``data`` holds an unknown key.
        """
        cls.check_known_keys(data, section)
        credentials = data.get("credentials", {})
        return cls(
            engine=data.get("engine"),
            credentials=dict(credentials),
            db_url=data.get("db_url"),
        )
