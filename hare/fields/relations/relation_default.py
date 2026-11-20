from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.models.model import Model


@dataclass(frozen=True, slots=True)
class RelationDefault:
    """A static related-row default without the source instance's graph or resources."""

    model: type[Model]
    key_values: tuple[tuple[str, Any], ...]

    @classmethod
    def capture(cls, obj: Model) -> RelationDefault:
        """Snapshot the declared saved row's primary key, including every composite component."""
        names = obj._binding.primary_key_attribute_names
        values = tuple((name, deepcopy(getattr(obj, name))) for name in names)
        if not obj._saved_in_db or not values or any(value is None for _, value in values):
            raise ConfigurationError("A static related-row default must be a saved instance with a complete key")
        return cls(type(obj), values)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(type(self)), [self.model, self.key_values], {}
