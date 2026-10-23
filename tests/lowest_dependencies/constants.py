from __future__ import annotations

from pathlib import Path

#: The repository root.
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[2]
#: The project file declaring hare's dependencies and the poetry dependency groups.
PYPROJECT_PATH = REPOSITORY_DIRECTORY / "pyproject.toml"
#: The poetry lock file, the versions the test tools are pinned to.
POETRY_LOCK_PATH = REPOSITORY_DIRECTORY / "poetry.lock"

#: The poetry dependency group of the tools the test suite runs with.
TEST_TOOL_GROUP = "test"
#: The name at the start of a PEP 508 requirement (``orjson (>=3.10)`` -> ``orjson``).
REQUIREMENT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*"
