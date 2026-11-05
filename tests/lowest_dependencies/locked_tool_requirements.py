"""Prints the test tools of the lowest-dependencies run as requirements pinned to their
``poetry.lock`` versions.

The run resolves hare's own dependencies - the ``[project]`` dependencies and every extra - to the
lowest versions their declared bounds allow (``uv pip compile --resolution lowest-direct``). The test
tools (the ``test`` poetry group, and the groups the test environments install - ``pytz``,
``pyright-cli``: the run takes the tests of every environment) aren't hare's dependencies and declare
no useful lower bound, so they are pinned to the versions the project is developed with; a tool that is
also one of hare's dependencies (``cryptography``, ``phonenumbers``, ``tzlocal``) stays at its lowest
version.

Usage:
    python -m tests.lowest_dependencies.locked_tool_requirements > test-tools.in
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.lowest_dependencies.constants import (
    PYPROJECT_PATH,
    REPOSITORY_DIRECTORY,
    TEST_TOOL_GROUP,
)
from tests.matrix.constants import SUITE_ENVIRONMENTS


class LockedToolRequirements:
    """The test tools pinned to their locked versions."""

    def __init__(self, pyproject_path: Path = PYPROJECT_PATH) -> None:
        with pyproject_path.open("rb") as pyproject_file:
            self.pyproject = tomllib.load(pyproject_file)

    def get_project_dependency_names(self) -> set[str]:
        """The normalized names of hare's dependencies, extras included."""
        project = self.pyproject["project"]
        requirements = list(project["dependencies"])
        for extra_requirements in project.get("optional-dependencies", {}).values():
            requirements.extend(extra_requirements)
        return {canonicalize_name(Requirement(requirement).name) for requirement in requirements}

    @staticmethod
    def get_tool_groups() -> list[str]:
        """The poetry groups of the test tools - the test group and the groups of the test environments."""
        environment_groups = (group for environment in SUITE_ENVIRONMENTS for group in environment.groups)
        return list(dict.fromkeys((TEST_TOOL_GROUP, *environment_groups)))

    def get_lines(self) -> list[str]:
        """Export locked tools with all platform/Python alternatives and their markers intact.

        Poetry interprets its lock format. Only direct tools are pinned here; project
        dependencies remain unconstrained so uv can select their declared lower bounds.
        The tool environment, including Poetry's export plugin, is separate from the
        target environment. Markers are evaluated by uv for that target, never here.
        """
        groups = self.get_tool_groups()
        exported = subprocess.run(
            [
                sys.executable,
                "-m",
                "poetry",
                "export",
                "--only",
                ",".join(groups),
                "--format",
                "requirements.txt",
                "--without-hashes",
            ],
            cwd=REPOSITORY_DIRECTORY,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return self.select_tools(exported.stdout)

    def select_tools(self, exported: str) -> list[str]:
        """Keep every exported alternative of a direct tool, preserving sources and markers."""
        poetry_groups = self.pyproject["tool"]["poetry"]["group"]
        tool_names = {
            canonicalize_name(tool_name)
            for group in self.get_tool_groups()
            for tool_name in poetry_groups[group]["dependencies"]
        } - self.get_project_dependency_names()
        lines = []
        found = set()
        for line in exported.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("--"):
                lines.append(line)
                continue
            name = canonicalize_name(Requirement(line).name)
            if name in tool_names:
                found.add(name)
                lines.append(line)
        if missing := tool_names - found:
            raise SystemExit(f"Test tools missing from Poetry's export: {', '.join(sorted(missing))}")
        return lines


if __name__ == "__main__":
    print("\n".join(LockedToolRequirements().get_lines()))
