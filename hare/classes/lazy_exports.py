from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any


class LazyExports:
    """The names a package exports from modules it imports on first use - a module importing the
    package back would otherwise meet it half loaded.

    Args:
        package_name: The package.
        exported_modules: The module of each exported name.
    """

    __slots__ = ("package_name", "exported_modules")

    def __init__(self, package_name: str, exported_modules: Mapping[str, str]) -> None:
        self.package_name = package_name
        self.exported_modules = exported_modules

    def get(self, name: str) -> Any:
        """An exported name - the package's ``__getattr__``.

        Args:
            name: The name.

        Returns:
            What the name's module holds under it.

        Raises:
            AttributeError: The package exports no such name.
        """
        module_name = self.exported_modules.get(name)
        if module_name is None:
            raise AttributeError(f"module {self.package_name!r} has no attribute {name!r}")
        return getattr(import_module(module_name), name)

    def get_names(self) -> list[str]:
        """The package's names - its own and the exported ones not imported yet: the package's
        ``__dir__``.

        Returns:
            The names, sorted.
        """
        return sorted({*vars(sys.modules[self.package_name]), *self.exported_modules})

    def get_module_hooks(self) -> tuple[Callable[[str], Any], Callable[[], list[str]]]:
        """The package's ``__getattr__`` and ``__dir__``.

        Returns:
            ``get()`` and ``get_names()``.
        """
        return self.get, self.get_names
