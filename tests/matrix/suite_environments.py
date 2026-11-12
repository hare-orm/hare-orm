from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from tests.matrix.constants import (
    EVERY_SUITE_ENVIRONMENT,
    NATIVE_MODULE,
    REQUIRES_MARKER,
    SUITE_ENVIRONMENT_OPTION,
    SUITE_ENVIRONMENTS,
)
from tests.matrix.matrix_catalog import MatrixCatalog
from tests.matrix.suite_environment import SuiteEnvironment
from tests.matrix.suite_requirements import SuiteRequirements


class SuiteEnvironments:
    """Which environment of ``SUITE_ENVIRONMENTS`` each test belongs to, and the tests of the one a run is
    in. The selection manifest declares module and directory import requirements without importing
    tests. A cell adds its declared requirements with ``@pytest.mark.requires("encryption")``.
    A test runs in the environment installing exactly the packages of what it requires - requiring
    pydantic and litestar is requiring litestar, whose extra installs pydantic."""

    #: The packages and groups the run's environment installs - set as pytest is configured.
    current_installed: ClassVar[frozenset[str]] = frozenset()
    #: Whether the run takes the tests of every environment (EVERY_SUITE_ENVIRONMENT) - a virtualenv
    #: installing everything.
    runs_every_environment: ClassVar[bool] = False

    @classmethod
    def set_current(cls, config: pytest.Config) -> None:
        """Takes the run's environment from pytest's options.

        Args:
            config: pytest's configuration.

        Raises:
            pytest.UsageError: No environment has the name given, two environments install the same, or
                rust.native isn't built.
        """
        try:
            SuiteRequirements.manifest.check_inventory()
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None
        name = config.getoption(SUITE_ENVIRONMENT_OPTION)
        cls.runs_every_environment = name == EVERY_SUITE_ENVIRONMENT
        environment = cls.get(name)
        requirements = SuiteRequirements.get_requirements(environment)
        cls.current_installed = SuiteRequirements.get_installed(requirements)
        environment_names_by_installation: dict[frozenset[str], str] = {}
        for known_environment in SUITE_ENVIRONMENTS:
            installation = SuiteRequirements.get_installed(SuiteRequirements.get_requirements(known_environment))
            if installation in environment_names_by_installation:
                raise pytest.UsageError(
                    f"the environments {environment_names_by_installation[installation]} and {known_environment.name} "
                    "install the same - their tests would run twice"
                )
            environment_names_by_installation[installation] = known_environment.name
        try:
            importlib.import_module(NATIVE_MODULE)
        except ImportError as error:
            raise pytest.UsageError(
                f"the {environment.name} environment needs {NATIVE_MODULE} - build it (make build_native)"
            ) from error

    @staticmethod
    def get(name: str) -> SuiteEnvironment:
        """The environment of a name.

        Args:
            name: The environment's name.

        Returns:
            The environment.

        Raises:
            pytest.UsageError: No environment has the name.
        """
        environments = MatrixCatalog.get_environments(f"{sys.version_info.major}.{sys.version_info.minor}")
        for environment in environments:
            if environment.name == name:
                return environment
        names = ", ".join(environment.name for environment in environments)
        raise pytest.UsageError(f"{SUITE_ENVIRONMENT_OPTION}={name} - the environments are: {names}")

    @classmethod
    def installs(cls, requirement: str) -> bool:
        """Whether the run's environment installs what an extra or group does - the litestar
        environment installs pydantic.

        Args:
            requirement: The extra or group.

        Returns:
            True when it does.
        """
        return SuiteRequirements.get_installed(frozenset({requirement})) <= cls.current_installed

    @staticmethod
    def get_path_requirements(path: Path) -> frozenset[str]:
        """What the tests of a file or directory require - ``SuiteRequirements.get_path_requirements()``.

        Args:
            path: A test module or a directory of tests.

        Returns:
            The names.

        Raises:
            pytest.UsageError: A test module is absent from the selection manifest.
        """
        try:
            return SuiteRequirements.get_path_requirements(path)
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None

    @classmethod
    def is_other_environment_path(cls, path: Path) -> bool:
        """Whether a module or directory needs what the run's environment doesn't install - pytest
        doesn't collect it, so its imports of packages the run lacks never run. A module requiring less
        than the environment installs is collected, its tests of other environments left out one by one.

        Args:
            path: The module or directory.

        Returns:
            True when it requires a package the run's environment doesn't install.
        """
        return not SuiteRequirements.get_installed(cls.get_path_requirements(path)) <= cls.current_installed

    @classmethod
    def get_other_environment_items(cls, items: list[pytest.Item]) -> list[pytest.Item]:
        """The tests of other environments than the run's.

        Args:
            items: The tests collected.

        Returns:
            The tests the run leaves out.

        Raises:
            pytest.UsageError: A test requires what no environment installs - it would run nowhere.
        """
        known_installed = {
            SuiteRequirements.get_installed(SuiteRequirements.get_requirements(environment))
            for environment in SUITE_ENVIRONMENTS
        }
        other_items = []
        for item in items:
            module_requirements = cls.get_path_requirements(item.path)
            if module_requirements:
                item.add_marker(getattr(pytest.mark, REQUIRES_MARKER)(*sorted(module_requirements)))
            requirements = module_requirements | {
                name for marker in item.iter_markers(REQUIRES_MARKER) for name in marker.args
            }
            try:
                SuiteRequirements.manifest.check_cell_requirements(item.path, requirements)
            except ValueError as error:
                raise pytest.UsageError(f"{item.nodeid}: {error}") from None
            installed = SuiteRequirements.get_installed(requirements)
            if installed not in known_installed:
                raise pytest.UsageError(
                    f"{item.nodeid} requires {sorted(requirements)} - no environment of SUITE_ENVIRONMENTS installs "
                    "exactly that; add one to tests/matrix/catalog.toml [[environments]]"
                )
            if cls.runs_every_environment:
                if not installed <= cls.current_installed:
                    other_items.append(item)
            elif installed != cls.current_installed:
                other_items.append(item)
        return other_items
