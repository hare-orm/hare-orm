from __future__ import annotations

import keyword
import sys
from types import ModuleType
from typing import Any

from hare.classes.constants import DECLARATIONS_MODULE_NAME


class ClassPath:
    """The dotted path a class (or a function) is imported by - what a migration file writes and
    reads back."""

    @staticmethod
    def get_module_name(named: Any) -> str:
        """The module a class or function is imported from: the package of its module when the
        module is named after it (``int_field.py`` holding ``IntField``) or is the package's
        ``declarations.py``, and the package exports it; else its own module.

        Args:
            named: The class or function.

        Returns:
            The module's dotted name - whatever ``__module__`` holds when it isn't a name.
        """
        module_name: str = named.__module__
        name = getattr(named, "__name__", None)
        if not module_name or not name:
            return module_name
        package_name, _separator, module_base_name = module_name.rpartition(".")
        if not package_name:
            return module_name
        if module_base_name != DECLARATIONS_MODULE_NAME and module_base_name.replace("_", "") != name.lower():
            return module_name
        package = sys.modules.get(package_name)
        # Read as an attribute: a package may hand its exports out on first use.
        if package is not None and getattr(package, name, None) is named:
            return package_name
        return module_name

    @staticmethod
    def is_importable_name(name: str) -> bool:
        """Whether the dotted name can be written in a Python import."""
        return all(part.isidentifier() and not keyword.iskeyword(part) for part in name.split("."))

    @staticmethod
    def has_module_identity(module: ModuleType | None, named: type) -> bool:
        """Whether the module holds this object at its qualified name."""
        found: Any = module
        for part in named.__qualname__.split("."):
            found = getattr(found, part, None)
            if found is None:
                return False
        return found is named

    @staticmethod
    def get(class_: Any) -> str:
        """The dotted path of a class or named function.

        Args:
            class_: The class or function.

        Returns:
            ``module.Name``.
        """
        module_name = ClassPath.get_module_name(class_)
        name = class_.__qualname__ if module_name == class_.__module__ else class_.__name__
        return f"{module_name}.{name}"
