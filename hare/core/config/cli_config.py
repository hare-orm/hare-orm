from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hare.core.config.config_section import ConfigSection
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class CliConfig(ConfigSection):
    """The config's ``cli`` section - read only by the ``hare`` command line."""

    #: Extra ``hare`` subcommands, as ``"package.module:ClassName"`` references.
    commands: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.commands, list) or not all(
            isinstance(reference, str) and reference for reference in self.commands
        ):
            raise ConfigurationError('Config "cli.commands" must be a list of non-empty "module:ClassName" strings')

    def to_dict(self) -> dict[str, Any]:
        return {"commands": list(self.commands)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], section: str = 'the "cli" section') -> Self:
        """Builds the section from its mapping form.

        Args:
            data: The mapping.
            section: What the mapping is, for error messages.

        Raises:
            ConfigurationError: If ``data`` isn't a mapping, holds an unknown key, or
                ``commands`` isn't a list of non-empty strings.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError('Config "cli" must be a mapping')
        cls.check_known_keys(data, section)
        return cls(commands=data.get("commands", []))
