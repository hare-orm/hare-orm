"""Prints the test tools of the lowest-dependencies run as requirements pinned to their
``poetry.lock`` versions.

The run resolves hare's own dependencies - the ``[project]`` dependencies and every extra - to the
lowest versions their declared bounds allow (``uv pip compile --resolution lowest-direct``). The test
tools (the ``test`` poetry group) aren't hare's dependencies and declare no useful lower bound, so
they are pinned to the versions the project is developed with; a tool that is also one of hare's
dependencies (``cryptography``, ``phonenumbers``, ``tzlocal``) stays at its lowest version.

Usage:
    python -m tests.lowest_dependencies.locked_tool_requirements > test-tools.in
"""

from __future__ import annotations

import re
import tomllib

from tests.lowest_dependencies.constants import (
    POETRY_LOCK_PATH,
    PYPROJECT_PATH,
    REQUIREMENT_NAME_PATTERN,
    TEST_TOOL_GROUP,
)


class LockedToolRequirements:
    """The test tools pinned to their locked versions."""

    def __init__(self) -> None:
        with PYPROJECT_PATH.open("rb") as pyproject_file:
            self.pyproject = tomllib.load(pyproject_file)
        with POETRY_LOCK_PATH.open("rb") as lock_file:
            self.lock = tomllib.load(lock_file)

    @staticmethod
    def normalize_name(name: str) -> str:
        """A package name in its PEP 503 normalized form."""
        return re.sub(r"[-_.]+", "-", name).lower()

    def get_project_dependency_names(self) -> set[str]:
        """The normalized names of hare's dependencies, extras included."""
        project = self.pyproject["project"]
        requirements = list(project["dependencies"])
        for extra_requirements in project.get("optional-dependencies", {}).values():
            requirements.extend(extra_requirements)
        names = set()
        for requirement in requirements:
            name_match = re.match(REQUIREMENT_NAME_PATTERN, requirement.strip())
            if name_match is None:
                raise SystemExit(f"Can't read the package name of {requirement!r} in pyproject.toml")
            names.add(self.normalize_name(name_match.group(0)))
        return names

    def get_lines(self) -> list[str]:
        """The pinned requirements, one per test tool that isn't one of hare's dependencies.

        Raises:
            SystemExit: A test tool isn't in ``poetry.lock``.
        """
        locked_versions = {
            self.normalize_name(package["name"]): package["version"] for package in self.lock["package"]
        }
        project_dependency_names = self.get_project_dependency_names()
        tool_names = self.pyproject["tool"]["poetry"]["group"][TEST_TOOL_GROUP]["dependencies"]
        lines = []
        for tool_name in tool_names:
            normalized_name = self.normalize_name(tool_name)
            if normalized_name in project_dependency_names:
                continue
            if normalized_name not in locked_versions:
                raise SystemExit(f"{tool_name} of the {TEST_TOOL_GROUP} group isn't in poetry.lock")
            lines.append(f"{normalized_name}=={locked_versions[normalized_name]}")
        return lines


if __name__ == "__main__":
    print("\n".join(LockedToolRequirements().get_lines()))
