from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, ClassVar

import hare
from hare.fields.field import Field
from tests.contracts.constants import FIELD_PACKAGES
from tests.matrix.constants import NATIVE_REQUIREMENT
from tests.matrix.suite_environments import SuiteEnvironments
from tests.utils.package_requirements import PackageRequirements


class HareRegistry:
    """What the contract tests enumerate - hare's modules and classes found by walking its packages, not
    listed by hand, so a class added to hare is enumerated at once."""

    #: The names of hare's modules, found on the first call.
    module_names: ClassVar[list[str]] = []

    @classmethod
    def get_module_names(cls) -> list[str]:
        """Every module of hare, found by its files - none of them imported.

        Returns:
            The dotted names, packages included.
        """
        if not cls.module_names:
            hare_directory = Path(hare.__file__).parent
            for path in sorted(hare_directory.rglob("*.py")):
                parts = path.relative_to(hare_directory.parent).with_suffix("").parts
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                cls.module_names.append(".".join(parts))
        return cls.module_names

    @staticmethod
    def is_installed(module_name: str) -> bool:
        """Whether the run's environment installs what a module of hare requires.

        Args:
            module_name: The module.

        Returns:
            True when it can import.
        """
        requirements = PackageRequirements.get(module_name)
        if NATIVE_REQUIREMENT in requirements and not SuiteEnvironments.current_native:
            return False
        return all(SuiteEnvironments.installs(name) for name in requirements if name != NATIVE_REQUIREMENT)

    @classmethod
    def get_installed_modules(cls) -> list[Any]:
        """Every module of hare the run's environment can import, imported.

        Returns:
            The modules.
        """
        return [importlib.import_module(name) for name in cls.get_module_names() if cls.is_installed(name)]

    @classmethod
    def get_field_classes(cls) -> list[type[Field[Any]]]:
        """Every field class of hare in the modules the run's environment imports.

        Returns:
            The classes, by their dotted path.
        """
        cls.get_installed_modules()
        found: dict[str, type[Field[Any]]] = {}
        pending: list[type[Field[Any]]] = [Field]
        while pending:
            field_class = pending.pop()
            if field_class.__module__.startswith("hare."):
                found[HareRegistry.get_path(field_class)] = field_class
            pending.extend(field_class.__subclasses__())
        return [found[path] for path in sorted(found)]

    @classmethod
    def get_exported_field_classes(cls) -> dict[str, type[Field[Any]]]:
        """The field classes the field packages export - a class itself, or the ``*FieldInstance`` class of a
        relation's or an enumeration's factory (``ForeignKeyField``, ``CharEnumField``).

        Returns:
            Each class by its dotted path.
        """
        classes_by_name = {field_class.__name__: field_class for field_class in cls.get_field_classes()}
        exported: dict[str, type[Field[Any]]] = {}
        for package_name in FIELD_PACKAGES:
            package = importlib.import_module(package_name)
            for name in package.__all__:
                value = getattr(package, name)
                if inspect.isclass(value) and issubclass(value, Field):
                    exported[HareRegistry.get_path(value)] = value
                elif callable(value) and f"{name}Instance" in classes_by_name:
                    instance_class = classes_by_name[f"{name}Instance"]
                    exported[HareRegistry.get_path(instance_class)] = instance_class
        return exported

    @staticmethod
    def get_path(value: type) -> str:
        """The dotted path of a class.

        Args:
            value: The class.

        Returns:
            Its module and name.
        """
        return f"{value.__module__}.{value.__qualname__}"
