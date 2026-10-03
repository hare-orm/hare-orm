from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class ConfigSection:
    """A part of the configuration written as a mapping - the whole config, one connection, one
    app, the ``cli`` section. Its keys are the dataclass's fields."""

    @classmethod
    def check_known_keys(cls, data: Mapping[str, Any], section: str) -> None:
        """Refuses a key that isn't one of the dataclass's fields - a misspelt key would otherwise
        be ignored silently, e.g. an app's ``default_conection`` leaving its models on the default
        connection.

        Args:
            data: The mapping.
            section: What the mapping is, for the error message (``"app 'models'"``).

        Raises:
            ConfigurationError: For the first unknown key, naming the closest known one.
        """
        known_keys = sorted(config_field.name for config_field in fields(cls))
        for key in data:
            if key in known_keys:
                continue
            close_matches = difflib.get_close_matches(str(key), known_keys, n=1)
            suggestion = f' - did you mean "{close_matches[0]}"?' if close_matches else ""
            raise ConfigurationError(
                f"Unknown key {key!r} in {section} - known keys: {', '.join(known_keys)}{suggestion}"
            )
