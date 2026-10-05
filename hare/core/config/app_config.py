from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Self


@dataclass(frozen=True)
class AppConfig(ConfigSection):
    models: Iterable[str | ModuleType]
    default_connection: str | None = None
    migrations: str | None = None

    def __post_init__(self) -> None:
        # Any iterable of dotted paths or modules, normalized to a list of module names. A bare
        # string isn't one.
        if isinstance(self.models, (str, bytes)):
            raise ConfigurationError(
                f"AppConfig.models must be a list/tuple of module paths, got str {self.models!r} - "
                f"write [{self.models!r}] instead"
            )
        try:
            raw_models = list(self.models)
        except TypeError as error:
            raise ConfigurationError(
                "AppConfig.models must be an iterable of strings or modules, not a single value"
            ) from error
        if not raw_models:
            raise ConfigurationError("AppConfig.models must be a non-empty list of strings")
        normalized_models: list[str] = []
        for model in raw_models:
            if isinstance(model, ModuleType):
                normalized_models.append(model.__name__)
            elif isinstance(model, str) and model:
                normalized_models.append(model)
            else:
                raise ConfigurationError("AppConfig.models must contain non-empty strings or modules")
        object.__setattr__(self, "models", normalized_models)
        if self.default_connection is not None and not isinstance(self.default_connection, str):
            raise ConfigurationError("AppConfig.default_connection must be a string or None")
        if self.migrations is not None and not isinstance(self.migrations, str):
            raise ConfigurationError("AppConfig.migrations must be a string or None")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"models": self.models}
        if self.default_connection is not None:
            data["default_connection"] = self.default_connection
        if self.migrations is not None:
            data["migrations"] = self.migrations
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = "an app") -> Self:
        """Builds the config from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages - ``HareConfig.from_dict()`` passes
                the app's name.

        Raises:
            ConfigurationError: If ``data`` isn't a mapping, holds an unknown key, or a value is
                invalid.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError("AppConfig must be created from a mapping")
        cls.check_known_keys(data, section)
        if "models" not in data:
            raise ConfigurationError('AppConfig requires "models"')
        # Left as-is (not coerced to list here) - AppConfig.__post_init__ itself accepts any
        # iterable of strings or modules and normalizes it.
        return cls(
            models=data["models"],
            default_connection=data.get("default_connection"),
            migrations=data.get("migrations"),
        )
