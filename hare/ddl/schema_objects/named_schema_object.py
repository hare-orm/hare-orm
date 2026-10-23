from __future__ import annotations

from dataclasses import dataclass

from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class NamedSchemaObject:
    """A named database object a model declares in its ``Meta`` beside its table - a view, a
    function, a sequence, a row level security policy.

    Args:
        name: The object's name - in the model's schema.

    Raises:
        ConfigurationError: The name isn't a non-empty string.
    """

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigurationError(f"{type(self).__name__}.name must be a non-empty string, got {self.name!r}")
