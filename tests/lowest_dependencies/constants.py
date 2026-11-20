from __future__ import annotations

from pathlib import Path

#: The repository root.
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[2]
#: The project file declaring hare's dependencies and the poetry dependency groups.
PYPROJECT_PATH = REPOSITORY_DIRECTORY / "pyproject.toml"

#: The poetry dependency group of the tools the test suite runs with.
TEST_TOOL_GROUP = "test"
