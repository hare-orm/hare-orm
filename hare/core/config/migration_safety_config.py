from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class MigrationSafetyConfig(ConfigSection):
    """The config's ``migrations.safety`` section - read by ``makemigrations`` and
    ``checkmigrations``."""

    #: The rows from which a table counts as large - an operation locking it is reported.
    large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS

    def to_dict(self) -> dict[str, Any]:
        return {"large_table_rows": self.large_table_rows}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = 'the "migrations.safety" section') -> Self:
        """Builds the section from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages.

        Raises:
            ConfigurationError: ``data`` holds an unknown key.
        """
        cls.check_known_keys(data, section)
        return cls(large_table_rows=data.get("large_table_rows", DEFAULT_LARGE_TABLE_ROWS))
