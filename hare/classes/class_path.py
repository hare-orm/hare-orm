from __future__ import annotations

import sys
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
        package_name, _separator, own_module_name = module_name.rpartition(".")
        if not package_name:
            return module_name
        if own_module_name != DECLARATIONS_MODULE_NAME and own_module_name.replace("_", "") != name.lower():
            return module_name
        package = sys.modules.get(package_name)
        # Read as an attribute: a package may hand its exports out on first use.
        if package is not None and getattr(package, name, None) is named:
            return package_name
        return module_name

    @staticmethod
    def get(class_: type) -> str:
        """The dotted path of a class.

        Args:
            class_: The class.

        Returns:
            ``module.Name``.
        """
        return f"{ClassPath.get_module_name(class_)}.{class_.__qualname__}"
