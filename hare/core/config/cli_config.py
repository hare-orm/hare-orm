from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class CliConfig(ConfigSection):
    """The config's ``cli`` section - read only by the ``hare`` command line."""

    #: Extra ``hare`` subcommands, as ``"package.module:ClassName"`` references.
    commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"commands": list(self.commands)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = 'the "cli" section') -> Self:
        """Builds the section from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages.

        Raises:
            ConfigurationError: ``data`` holds an unknown key.
        """
        cls.check_known_keys(data, section)
        return cls(commands=data.get("commands", []))
