from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import ClassVar

from tests.matrix.constants import REPOSITORY_DIRECTORY, REQUIRES_MODULE_NAME, TESTS_DIRECTORY
from tests.matrix.suite_environment import SuiteEnvironment


class SuiteRequirements:
    """What the tests of a module or an environment require and what that installs - read from the sources
    and pyproject.toml with the standard library alone, so the matrix runner, which runs without pytest,
    reads them too."""

    #: ``REQUIRES`` of each module read.
    module_requirements: ClassVar[dict[Path, frozenset[str]]] = {}
    #: The packages each extra of hare installs, read from pyproject.toml.
    extra_packages: ClassVar[dict[str, frozenset[str]]] = {}

    @staticmethod
    def get_requirements(environment: SuiteEnvironment) -> frozenset[str]:
        """What a test of an environment requires - its extras and groups.

        Args:
            environment: The environment.

        Returns:
            The extras and groups.
        """
        return frozenset((*environment.extras, *environment.groups))

    @classmethod
    def get_extra_packages(cls) -> dict[str, frozenset[str]]:
        """The packages each extra of hare installs - the litestar extra lists pydantic among them.

        Returns:
            The requirement texts of each extra.
        """
        if not cls.extra_packages:
            with (REPOSITORY_DIRECTORY / "pyproject.toml").open("rb") as pyproject_file:
                extras = tomllib.load(pyproject_file)["project"]["optional-dependencies"]
            cls.extra_packages = {
                extra: frozenset(requirement.strip() for requirement in requirements)
                for extra, requirements in extras.items()
            }
        return cls.extra_packages

    @classmethod
    def get_installed(cls, requirements: frozenset[str]) -> frozenset[str]:
        """What installing extras and groups installs - a group is itself.

        Args:
            requirements: The extras and groups.

        Returns:
            The packages of the extras, and the groups.
        """
        extra_packages = cls.get_extra_packages()
        installed: set[str] = set()
        for name in requirements:
            installed |= extra_packages.get(name, frozenset({name}))
        return frozenset(installed)

    @classmethod
    def get_module_requirements(cls, path: Path) -> frozenset[str]:
        """``REQUIRES`` of a module - read from its source, not imported.

        Args:
            path: The module's file.

        Returns:
            The names it requires, none without the declaration.

        Raises:
            ValueError: ``REQUIRES`` isn't a tuple of texts.
        """
        cached_requirements = cls.module_requirements.get(path)
        if cached_requirements is not None:
            return cached_requirements
        requirements: frozenset[str] = frozenset()
        if path.is_file():
            for statement in ast.parse(path.read_text(encoding="utf-8")).body:
                if not (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == REQUIRES_MODULE_NAME
                        for target in statement.targets
                    )
                ):
                    continue
                try:
                    value = ast.literal_eval(statement.value)
                except ValueError:
                    value = None
                if not (isinstance(value, tuple) and all(isinstance(name, str) for name in value)):
                    raise ValueError(f"{path}: {REQUIRES_MODULE_NAME} is a tuple of extras and groups")
                requirements = frozenset(value)
        cls.module_requirements[path] = requirements
        return requirements

    @classmethod
    def get_path_requirements(cls, path: Path) -> frozenset[str]:
        """What the tests of a file or directory require - its own ``REQUIRES`` and those of the
        ``conftest.py`` of each directory above it, up to the test suite's.

        Args:
            path: A test module or a directory of tests.

        Returns:
            The names.

        Raises:
            ValueError: A ``REQUIRES`` isn't a tuple of texts.
        """
        requirements = cls.get_module_requirements(path) if path.suffix == ".py" else frozenset()
        directory = path if path.is_dir() else path.parent
        while directory.is_relative_to(TESTS_DIRECTORY):
            requirements |= cls.get_module_requirements(directory / "conftest.py")
            directory = directory.parent
        return requirements
