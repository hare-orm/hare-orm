from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.factories.model_factory import ModelFactory


class FactoryReference:
    """A factory named by its class or its dotted path - two factories naming each other import
    each other's module only once they make an object.

    Args:
        factory: The factory, or its dotted path.
    """

    def __init__(self, factory: type[ModelFactory[Any]] | str) -> None:
        self.factory = factory

    def get_factory(self) -> type[ModelFactory[Any]]:
        """The factory, imported on the first call for a dotted path."""
        factory = self.factory
        if isinstance(factory, str):
            module_name, _, factory_name = factory.rpartition(".")
            factory = self.factory = getattr(importlib.import_module(module_name), factory_name)
        return factory
