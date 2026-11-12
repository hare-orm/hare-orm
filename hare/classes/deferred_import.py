from __future__ import annotations

import importlib
from typing import Any, Generic, TypeVar

TImported = TypeVar("TImported")


class DeferredImport(Generic[TImported]):
    """A class attribute holding an object of another module, imported at its first read - for two
    modules importing each other. The first read puts the object itself on the class in place of this
    attribute, so every later read is a plain class attribute.

    Args:
        module_name: The module holding the object.
        object_name: The object's name in it.
    """

    __slots__ = ("module_name", "object_name", "owner", "attribute_name")

    def __init__(self, module_name: str, object_name: str) -> None:
        self.module_name = module_name
        self.object_name = object_name
        self.owner: type | None = None
        self.attribute_name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.owner = owner
        self.attribute_name = name

    def install_on(self, owner: type, name: str) -> None:
        """Attach after a decorator has inspected class defaults, without importing the object.

        Args:
            owner: The class holding the deferred attribute.
            name: Its attribute name.
        """
        self.__set_name__(owner, name)
        setattr(owner, name, self)

    def __get__(self, instance: Any, owner: type | None = None) -> TImported:
        imported: TImported = getattr(importlib.import_module(self.module_name), self.object_name)
        setattr(self.owner, self.attribute_name, imported)
        return imported
