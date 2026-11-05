from __future__ import annotations

from pathlib import Path

import pytest

from hare.core.config import HareConfig
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels
from tests.contrib.mypy.hare_config import HARE_CONFIG
from tests.contrib.mypy.postgresql_hare_config import HARE_CONFIG as POSTGRESQL_HARE_CONFIG
from tests.stubs.stub_configs import (
    MANAGERS_CONFIG,
    MYPY_MODELS_STUB_PATH,
    POSTGRESQL_MODELS_STUB_PATH,
    write_stubs,
)


@pytest.fixture(scope="module")
def stubs_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stubs of CONFIG's models - written once for the tests of a module reading them."""
    output_directory = tmp_path_factory.mktemp("stubs")
    write_stubs(output_directory)
    return output_directory


@pytest.fixture(scope="module")
def manager_stubs_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stubs of MANAGERS_CONFIG's models - written once for the tests of a module reading them."""
    output_directory = tmp_path_factory.mktemp("manager_stubs")
    StubWriter(BoundModels(HareConfig.load(MANAGERS_CONFIG)), output_directory, 1).write()
    return output_directory


@pytest.fixture(scope="module")
def mypy_models_stub_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The stub of the mypy plugin's test models - written once for the tests of a module reading it."""
    output_directory = tmp_path_factory.mktemp("mypy_models_stubs")
    StubWriter(BoundModels(HareConfig.load(HARE_CONFIG)), output_directory, 1).write()
    return output_directory.joinpath(*MYPY_MODELS_STUB_PATH).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def postgresql_models_stub_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The stub of the PostgreSQL typing models - written once for the tests of a module reading it."""
    output_directory = tmp_path_factory.mktemp("postgresql_models_stubs")
    StubWriter(BoundModels(HareConfig.load(POSTGRESQL_HARE_CONFIG)), output_directory, 1).write()
    return output_directory.joinpath(*POSTGRESQL_MODELS_STUB_PATH).read_text(encoding="utf-8")
