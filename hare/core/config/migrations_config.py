from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection
from hare.core.config.migration_safety_config import MigrationSafetyConfig
from hare.exceptions import ConfigurationError
from hare.transactions.constants import (
    MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS,
    MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class MigrationsConfig(ConfigSection):
    """The config's ``migrations`` section - read by ``migrate``, ``makemigrations`` and
    ``checkmigrations``."""

    #: Seconds a migration's statement waits for a lock another session holds before the migration
    #: fails - None to wait as long as the database does. ``migrate(lock_timeout=...)`` and
    #: ``hare migrate --lock-timeout`` take precedence.
    lock_timeout: float | None = None
    #: The migration safety check's settings - its defaults when None.
    safety: MigrationSafetyConfig | None = None

    def __post_init__(self) -> None:
        if self.safety is not None and not isinstance(self.safety, MigrationSafetyConfig):
            raise ConfigurationError("MigrationsConfig.safety must be a MigrationSafetyConfig or None")
        lock_timeout: Any = self.lock_timeout
        if lock_timeout is None:
            return
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, int | float):
            raise ConfigurationError(
                f'Config "migrations.lock_timeout" must be a number of seconds, got {lock_timeout!r}'
            )
        if not MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS <= lock_timeout <= MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS:
            raise ConfigurationError(
                f'Config "migrations.lock_timeout" must be between {MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS} and '
                f"{MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS} seconds, got {lock_timeout!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        config: dict[str, Any] = {} if self.lock_timeout is None else {"lock_timeout": self.lock_timeout}
        if self.safety is not None:
            config["safety"] = self.safety.to_dict()
        return config

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = 'the "migrations" section') -> Self:
        """Builds the section from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages.

        Raises:
            ConfigurationError: ``data`` isn't a mapping, holds an unknown key, ``lock_timeout``
                isn't a number of seconds within the supported range, or ``safety`` isn't valid.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError('Config "migrations" must be a mapping')
        cls.check_known_keys(data, section)
        safety_section = data.get("safety")
        safety = MigrationSafetyConfig.from_dict(safety_section) if safety_section is not None else None
        return cls(lock_timeout=data.get("lock_timeout"), safety=safety)
