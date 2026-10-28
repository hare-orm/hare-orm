from __future__ import annotations

import importlib
from pathlib import Path
from typing import ClassVar

import pytest

from tests.matrix.constants import (
    EVERY_SUITE_ENVIRONMENT,
    NATIVE_MODULE,
    NATIVE_REQUIREMENT,
    REQUIRES_MARKER,
    SUITE_ENVIRONMENT_OPTION,
    SUITE_ENVIRONMENTS,
)
from tests.matrix.suite_environment import SuiteEnvironment
from tests.matrix.suite_requirements import SuiteRequirements


class SuiteEnvironments:
    """Which environment of ``SUITE_ENVIRONMENTS`` each test belongs to, and the tests of the one a run is
    in. A test module declares what it requires in ``REQUIRES = ("litestar",)`` - read without importing
    it, so a run without the package doesn't import it either - and so does a ``conftest.py`` for its
    directory; a test adds its own with ``@pytest.mark.requires("encryption")``. A test runs in the
    environment installing exactly the packages of what it requires - requiring pydantic and litestar is
    requiring litestar, whose extra installs pydantic - and, unless it requires ``native`` (hare's
    ``rust.native`` extension), in the environment of the same packages without the extension."""

    #: What the tests of the run's environment require - set as pytest is configured.
    current_requirements: ClassVar[frozenset[str]] = frozenset()
    #: The packages and groups the run's environment installs - set as pytest is configured.
    current_installed: ClassVar[frozenset[str]] = frozenset()
    #: Whether the run's environment has rust.native - set as pytest is configured.
    current_native: ClassVar[bool] = True
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
                rust.native is there in an environment without it, or missing in one with it.
        """
        name = config.getoption(SUITE_ENVIRONMENT_OPTION)
        cls.runs_every_environment = name == EVERY_SUITE_ENVIRONMENT
        if cls.runs_every_environment:
            environment = SuiteEnvironment(
                EVERY_SUITE_ENVIRONMENT,
                tuple(dict.fromkeys(extra for known in SUITE_ENVIRONMENTS for extra in known.extras)),
                tuple(dict.fromkeys(group for known in SUITE_ENVIRONMENTS for group in known.groups)),
            )
        else:
            environment = cls.get(name)
        cls.current_requirements = SuiteRequirements.get_requirements(environment)
        cls.current_installed = SuiteRequirements.get_installed(cls.current_requirements)
        cls.current_native = environment.native
        environment_names_by_installation: dict[tuple[frozenset[str], bool], str] = {}
        for known_environment in SUITE_ENVIRONMENTS:
            installation = (
                SuiteRequirements.get_installed(SuiteRequirements.get_requirements(known_environment)),
                known_environment.native,
            )
            if installation in environment_names_by_installation:
                raise pytest.UsageError(
                    f"the environments {environment_names_by_installation[installation]} and {known_environment.name} "
                    "install the same - their tests would run twice"
                )
            environment_names_by_installation[installation] = known_environment.name
        try:
            importlib.import_module(NATIVE_MODULE)
        except ImportError as error:
            if environment.native:
                raise pytest.UsageError(
                    f"the {environment.name} environment needs {NATIVE_MODULE} - build it (make build_native)"
                ) from error
        else:
            if not environment.native:
                raise pytest.UsageError(
                    f"the {environment.name} environment has no {NATIVE_MODULE} - run it with "
                    "-p tests.matrix.without_native"
                )

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
        for environment in SUITE_ENVIRONMENTS:
            if environment.name == name:
                return environment
        names = ", ".join(environment.name for environment in SUITE_ENVIRONMENTS)
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
            pytest.UsageError: A ``REQUIRES`` isn't a tuple of texts.
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
        if cls.runs_every_environment:
            return False
        requirements = cls.get_path_requirements(path)
        if NATIVE_REQUIREMENT in requirements and not cls.current_native:
            return True
        return not SuiteRequirements.get_installed(requirements - {NATIVE_REQUIREMENT}) <= cls.current_installed

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
            requirements = cls.get_path_requirements(item.path) | {
                name for marker in item.iter_markers(REQUIRES_MARKER) for name in marker.args
            }
            installed = SuiteRequirements.get_installed(requirements - {NATIVE_REQUIREMENT})
            if installed not in known_installed:
                raise pytest.UsageError(
                    f"{item.nodeid} requires {sorted(requirements)} - no environment of SUITE_ENVIRONMENTS installs "
                    "exactly that; add one to tests/matrix/constants.py"
                )
            if cls.runs_every_environment:
                continue
            if installed != cls.current_installed or (NATIVE_REQUIREMENT in requirements and not cls.current_native):
                other_items.append(item)
        return other_items
