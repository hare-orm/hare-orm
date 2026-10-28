from __future__ import annotations

import builtins
import sys
from typing import Any


class StubImports:
    """The names a stub's added declarations import, rendered as ``from module import Name`` lines.

    Args:
        module_name: The module the stub is of - its own names need no import.
    """

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        #: The names to import, by module - ``Name`` or ``Name as Alias``.
        self.names_by_module: dict[str, set[str]] = {}
        #: The name each imported name is written as, by (module, name).
        self.written_names: dict[tuple[str, str], str] = {}
        module = sys.modules.get(module_name)
        #: The names the stub's own module binds - an imported name taking one of them is aliased.
        self.module_names: set[str] = set(vars(module)) if module is not None else set()
        #: The names of the declarations the stub adds (``<Model>Filters``, ...).
        self.declared_names: set[str] = set()

    def declare(self, name: str) -> str:
        """Takes a name for a declaration the stub adds - one the module, an import or another
        declaration holds already gets an underscore after it, until it is free.

        Args:
            name: The name wanted.

        Returns:
            The name the declaration gets.
        """
        while name in self.module_names or name in self.declared_names or name in self.written_names.values():
            name += "_"
        self.declared_names.add(name)
        return name

    def add(self, module_name: str, name: str) -> str:
        """Imports a name - nothing for an unshadowed builtin or a name of the stub's own module. A name the stub
        already has (a class of its own of that name, another module's of that name imported before)
        is imported under an alias.

        Args:
            module_name: The module the name is in.
            name: The name.

        Returns:
            How the stub writes the name.
        """
        if module_name == self.module_name:
            return name
        written_name = self.written_names.get((module_name, name))
        if written_name is not None:
            return written_name
        module = sys.modules.get(module_name)
        stub_binding = vars(sys.modules[self.module_name]).get(name) if self.module_name in sys.modules else None
        taken = (
            name in self.written_names.values()
            or name in self.declared_names
            or (name in self.module_names and (module is None or stub_binding is not vars(module).get(name)))
        )
        written_name = f"{module_name.replace('.', '_')}_{name}" if taken else name
        if taken:
            while (
                written_name in self.module_names
                or written_name in self.declared_names
                or written_name in self.written_names.values()
            ):
                written_name += "_"
        self.written_names[(module_name, name)] = written_name
        if module_name != builtins.__name__ or written_name != name:
            self.names_by_module.setdefault(module_name, set()).add(
                name if written_name == name else f"{name} as {written_name}"
            )
        return written_name

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
