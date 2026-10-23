import importlib.metadata as importlib_metadata
import os
import re
import shutil
import subprocess  # nosec
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

import pytest

from hare import __version__


def _load_version():
    return importlib_metadata.version("hare-orm")


@pytest.mark.database_independent
def test_version():
    assert _load_version() == __version__


def run_shell(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, **kw)  # nosec


def _capture_output(cmd: list[str], **kw) -> str:
    return run_shell(cmd, text=True, capture_output=True, encoding="utf-8", **kw).stdout


@unittest.skipIf(
    os.getenv("HARE_TEST_POETRY_ADD", "").lower() not in ("1", "true", "yes", "on"),
    "Env 'HARE_TEST_POETRY_ADD' is not true",
)
def test_added_by_poetry_v2():
    hare_orm = Path(__file__).resolve().parents[2]
    py = "{}.{}".format(*sys.version_info)
    # The arguments go as a list - a Windows path split out of a command line loses its backslashes.
    poetry = ["poetry"] if shutil.which("poetry") is not None else ["uvx", "poetry"]
    # The new project's poetry works in the project's own virtualenv - not in the one of this checkout,
    # which `poetry run pytest` has active.
    env = {name: value for name, value in os.environ.items() if name not in ("VIRTUAL_ENV", "POETRY_ACTIVE")}
    # The project sits on the checkout's drive: poetry writes the path to hare-orm relative to the
    # project, and Windows has no relative path from one drive to another.
    with tempfile.TemporaryDirectory(dir=hare_orm.parent, ignore_cleanup_errors=True) as project_root:
        with chdir(project_root):
            package = "foo"
            run_shell([*poetry, "new", package, f"--python=^{py}"], env=env)  # nosec
            with chdir(package):
                run_shell([*poetry, "config", "--local", "virtualenvs.in-project", "true"], env=env)  # nosec
                run_shell([*poetry, "env", "use", py], env=env)  # nosec
                r = run_shell([*poetry, "add", str(hare_orm)], env=env)  # nosec
                assert r.returncode == 0
                out = _capture_output([*poetry, "run", "pip", "list"], env=env)  # nosec
                assert re.search(rf"hare-orm\s*{__version__}", out)
