"""``hare stubs``: the text of a stub is the same in every process, whatever its hash seed."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.stubs.stub_configs import REPOSITORY_PATH

pytestmark = pytest.mark.database_independent

STUB_WRITING_SCRIPT = """
import sys
from pathlib import Path

from hare.core.config import HareConfig
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels

config = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.shared_models.testmodels"]}},
}
# stubgen inspects modules in a child process, which imports this script again.
if __name__ == "__main__":
    StubWriter(BoundModels(HareConfig.load(config)), Path(sys.argv[1]), 2).write()
"""


def test_a_stub_is_the_same_text_whatever_the_hash_seed(tmp_path):
    # A model's relations are a set - written in the order the model declares them, not the set's.
    script = tmp_path / "write_stubs.py"
    script.write_text(STUB_WRITING_SCRIPT, encoding="utf-8")
    seeds = ("1", "2", "3")
    # The three processes run at once - each one writes the stubs of its own hash seed.
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(tmp_path / f"typings_{seed}")],
            cwd=REPOSITORY_PATH,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPOSITORY_PATH)},
        )
        for seed in seeds
    ]
    assert [process.wait() for process in processes] == [0, 0, 0]
    stub_texts = [
        (tmp_path / f"typings_{seed}").joinpath("tests", "shared_models", "testmodels.pyi").read_text(encoding="utf-8")
        for seed in seeds
    ]
    assert stub_texts[0] == stub_texts[1] == stub_texts[2]
