from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS, MAX_LARGE_TABLE_ROWS
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class MigrationSafetyConfig(ConfigSection):
    """The config's ``migrations.safety`` section - read by ``makemigrations`` and
    ``checkmigrations``."""

    #: The rows from which a table counts as large - an operation locking it is reported.
    large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS

    def __post_init__(self) -> None:
        large_table_rows: Any = self.large_table_rows
        if isinstance(large_table_rows, bool) or not isinstance(large_table_rows, int):
            raise ConfigurationError(
                f'Config "migrations.safety.large_table_rows" must be a whole number of rows, got {large_table_rows!r}'
            )
        if not 0 <= large_table_rows <= MAX_LARGE_TABLE_ROWS:
            raise ConfigurationError(
                f'Config "migrations.safety.large_table_rows" must be between 0 and {MAX_LARGE_TABLE_ROWS}, '
                f"got {large_table_rows!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"large_table_rows": self.large_table_rows}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = 'the "migrations.safety" section') -> Self:
        """Builds the section from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages.

        Raises:
            ConfigurationError: ``data`` isn't a mapping, holds an unknown key, or
                ``large_table_rows`` isn't a whole number within the supported range.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError('Config "migrations.safety" must be a mapping')
        cls.check_known_keys(data, section)
        return cls(large_table_rows=data.get("large_table_rows", DEFAULT_LARGE_TABLE_ROWS))
