from __future__ import annotations

import tomllib
from pathlib import Path
from typing import ClassVar

from tests.matrix.constants import NATIVE_REQUIREMENT, SUITE_ENVIRONMENTS
from tests.matrix.paths import REPOSITORY_DIRECTORY
from tests.matrix.suite_environment import SuiteEnvironment
from tests.matrix.test_selection_manifest import TestSelectionManifest


class SuiteRequirements:
    """What the tests of a module or an environment require and what that installs - read from the manifest
    and pyproject.toml with the standard library alone, so the matrix runner, which runs without pytest,
    reads them too."""

    manifest: ClassVar[TestSelectionManifest] = TestSelectionManifest.read(
        REPOSITORY_DIRECTORY / "tests/matrix/selection.toml"
    )
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
    def get_path_requirements(cls, path: Path) -> frozenset[str]:
        """Get a path's declared import requirements without evaluating Python sources."""
        return cls.manifest.get_path_requirements(path)

    @classmethod
    def get_test_paths(cls, environment: SuiteEnvironment) -> tuple[str, ...]:
        """Select candidate modules from their declared optional cell requirements."""
        installed = cls.get_installed(cls.get_requirements(environment))
        known_names = {name for known in SUITE_ENVIRONMENTS for name in (*known.extras, *known.groups)}
        environment_names = {name for name in known_names if cls.get_installed(frozenset({name})) <= installed}
        return tuple(
            path
            for path, module in sorted(cls.manifest.modules.items())
            if (module.cells | cls.get_path_requirements(REPOSITORY_DIRECTORY / path)) & environment_names
            and cls.get_installed(cls.get_path_requirements(REPOSITORY_DIRECTORY / path) - {NATIVE_REQUIREMENT})
            <= installed
        )
