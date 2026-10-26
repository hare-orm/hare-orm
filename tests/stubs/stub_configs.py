"""The configurations the tests of ``hare stubs`` write stubs of, and where each stub lands."""

from __future__ import annotations

from pathlib import Path

from hare.core.config import HareConfig
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels

CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.factories_models"]}},
}
STUB_PATH = ("tests", "contrib", "factories_models.pyi")
MANAGERS_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.stubs.stub_managers_models"]}},
}
MANAGERS_STUB_PATH = ("tests", "stubs", "stub_managers_models.pyi")
MYPY_MODELS_STUB_PATH = ("tests", "contrib", "mypy", "models.pyi")
POSTGRESQL_MODELS_STUB_PATH = ("tests", "contrib", "mypy", "postgresql_models.pyi")
#: The repository root - pyright reads the modules of the stubs' models from it.
REPOSITORY_PATH = Path(__file__).resolve().parents[2]


def write_stubs(output_directory: Path) -> Path:
    StubWriter(BoundModels(HareConfig.load(CONFIG)), output_directory, 2).write()
    return output_directory.joinpath(*STUB_PATH)
