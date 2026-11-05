from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from hare.classes.class_path import ClassPath
from hare.declarations.messages import ENVIRONMENT_VARIABLE_NOT_SET
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class EnvironmentValue:
    """An environment reference saved in declarations without reading its current value.

    Consumers read the value only when executing their configuration. Serialization
    and repr contain the variable name, so a migration never captures its contents.
    """

    name: str

    def get_value(self) -> str:
        """Read the current environment; missing variables are reported without exposing values."""
        try:
            return os.environ[self.name]
        except KeyError:
            raise ConfigurationError(ENVIRONMENT_VARIABLE_NOT_SET.format(name=self.name)) from None

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(type(self)), [self.name], {}
