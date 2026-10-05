from __future__ import annotations

import builtins
from typing import Any


class StubImports:
    """The names a stub's added declarations import, rendered as ``from module import Name`` lines.

    Args:
        module_name: The module the stub is of - its own names need no import.
    """

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        #: The names to import, by module.
        self.names_by_module: dict[str, set[str]] = {}

    def add(self, module_name: str, name: str) -> str:
        """Imports a name - nothing for a builtin or a name of the stub's own module.

        Args:
            module_name: The module the name is in.
            name: The name.

        Returns:
            How the stub writes the name.
        """
        if module_name != self.module_name and module_name != builtins.__name__:
            self.names_by_module.setdefault(module_name, set()).add(name)
        return name

    def add_class(self, imported_class: Any) -> str:
        """Imports a class by its qualified name's first part.

        Args:
            imported_class: The class.

        Returns:
            How the stub writes the class.
        """
        top_name, _, rest = imported_class.__qualname__.partition(".")
        written = self.add(imported_class.__module__, top_name)
        return f"{written}.{rest}" if rest else written

    def render(self) -> list[str]:
        """The import lines, sorted."""
        return [
            f"from {module_name} import {', '.join(sorted(names))}"
            for module_name, names in sorted(self.names_by_module.items())
        ]
