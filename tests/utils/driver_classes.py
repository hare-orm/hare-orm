from __future__ import annotations

import importlib
from typing import Any

import pytest
from _pytest.mark import ParameterSet

from tests.utils.package_requirements import PackageRequirements


class DriverClasses:
    """Classes of hare's drivers as parameters of a test running on each of them - named by their dotted
    path, imported by the ``client_class`` fixture (``indirect=True``). A class of a driver needing a package
    requires its extra: the test's module imports without the package, each parameter runs in the
    environment of its driver."""

    @staticmethod
    def get_parameters(*paths: str) -> list[ParameterSet]:
        """The parameters of the classes.

        Args:
            paths: The dotted paths of the classes.

        Returns:
            One parameter per class, named after it.
        """
        return [DriverClasses.get_parameter(path) for path in paths]

    @staticmethod
    def get_parameter(path: str, *values: Any) -> ParameterSet:
        """The parameter of a class, with the other values of the test's parameters.

        Args:
            path: The dotted path of the class.
            values: The values of the test's other parameters.

        Returns:
            The parameter, named after the class and the values that are numbers or texts.
        """
        extras = PackageRequirements.get(path)
        marks = [pytest.mark.requires(*extras)] if extras else []
        named_values = [str(value) for value in values if isinstance(value, (str, int, float)) or value is None]
        identifier = "-".join([path.rsplit(".", 1)[1], *named_values])
        return pytest.param(path, *values, id=identifier, marks=marks)

    @staticmethod
    def get_class(path: str) -> Any:
        """The class of a dotted path.

        Args:
            path: The path.

        Returns:
            The class.
        """
        module_name, class_name = path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)
