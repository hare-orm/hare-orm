from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from hare.classes.class_path import ClassPath
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class Dictionary(NamedSchemaObject):
    """A dictionary a model declares in ``Meta.dictionaries`` - rows of the model's table the
    database keeps loaded for looking a value up by a key. A dialect with dictionaries declares its
    own class of them.

    Args:
        name: The dictionary's name.
        key: The fields of the model a row is looked up by.
        attributes: The fields of the model a lookup gives.

    Raises:
        ConfigurationError: The name is empty; ``key`` or ``attributes`` isn't a non-empty sequence of
            distinct field names, or the two share a field.
    """

    key: tuple[str, ...]
    attributes: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        for option_name in ("key", "attributes"):
            field_names = getattr(self, option_name)
            if (
                isinstance(cast("object", field_names), str)
                or not field_names
                or not all(isinstance(field_name, str) and field_name for field_name in field_names)
            ):
                raise ConfigurationError(
                    f"{type(self).__name__} {self.name!r}: {option_name} must be a non-empty sequence of field "
                    f"names, got {field_names!r}"
                )
            object.__setattr__(self, option_name, tuple(field_names))
        field_names = self.get_field_names()
        if len(set(field_names)) != len(field_names):
            raise ConfigurationError(
                f"{type(self).__name__} {self.name!r}: key and attributes name a field twice: {field_names!r}"
            )

    def get_field_names(self) -> tuple[str, ...]:
        """The fields of the model the dictionary reads.

        Returns:
            The key's fields, then the attributes.
        """
        return (*self.key, *self.attributes)

    def get_options(self) -> dict[str, Any]:
        """The arguments beside the name, the key and the attributes a migration file writes."""
        return {}

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """How a migration file rebuilds the dictionary.

        Returns:
            The class path, no positional arguments, and the keyword arguments.
        """
        arguments = {"name": self.name, "key": self.key, "attributes": self.attributes, **self.get_options()}
        return ClassPath.get(type(self)), [], arguments
