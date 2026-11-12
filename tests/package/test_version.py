import importlib.metadata as importlib_metadata
import os
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
    return subprocess.run(cmd, check=True, **kw)  # nosec


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
            # Test the selected minor without claiming unsupported future Python versions.
            run_shell([*poetry, "new", package, f"--python=~{py}"], env=env)  # nosec
            with chdir(package):
                run_shell([*poetry, "config", "--local", "virtualenvs.in-project", "true"], env=env)  # nosec
                run_shell([*poetry, "env", "use", sys.executable], env=env)  # nosec
                run_shell([*poetry, "add", str(hare_orm)], env=env)  # nosec
                # Read the installed distribution and import it without the checkout's PYTHONPATH.
                source = (
                    "import sys; from importlib.metadata import version; import hare; "
                    "print(version('hare-orm')); print(hare.__version__); "
                    "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                )
                result = run_shell(
                    [*poetry, "run", "python", "-I", "-c", source],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    encoding="utf-8",
                )
                assert result.stdout.splitlines() == [__version__, __version__, py]
