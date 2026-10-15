"""End-to-end regression test for a fixed (non-templated) sqlite `hare_test_context(db_url=...)`
under real pytest-xdist workers - see DbUrlConfigGenerator.expand()'s own xdist-worker-suffix logic
in hare/backends/base/config_generator.py.

This spawns a real `pytest -n 2` subprocess (rather than asserting on DbUrlConfigGenerator.expand()
directly, as tests/backends/test_db_url.py already does) because the bug only reproduces with two
actual OS processes racing for the same sqlite file - a single-process test can't observe
sqlite3.OperationalError("database is locked") from cross-process contention.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.database_independent

REPO_ROOT = Path(__file__).resolve().parent.parent

_WORKER_TEST_MODULE = textwrap.dedent(
    """
    import asyncio
    import pytest

    from hare import fields
    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.models import Model


    class Tournament(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=10)


    @pytest.mark.parametrize("i", range(6))
    def test_fixed_path_concurrent_workers(i, shared_db_path):
        async def run():
            async with hare_test_context(["test_worker_module"], db_url=f"sqlite+aiosqlite:///{shared_db_path}"):
                for _ in range(10):
                    await Tournament.objects.create(name="x")

        asyncio.run(run())
    """
)


@pytest.mark.slow
def test_fixed_sqlite_path_survives_real_xdist_run(tmp_path):
    """Before the fix: every worker's `hare_test_context(db_url="sqlite+aiosqlite:///<fixed path>")` call
    hits the SAME literal file, and this subprocess run fails with sqlite3.OperationalError
    ("database is locked"). After the fix, DbUrlConfigGenerator.expand() folds the
    PYTEST_XDIST_WORKER id into the path automatically, so each worker gets its own file and the
    run passes."""
    shared_db_path = (tmp_path / "fixed_shared.db").as_posix()
    worker_test_file = tmp_path / "test_worker_module.py"
    worker_test_file.write_text(_WORKER_TEST_MODULE)
    conftest_file = tmp_path / "conftest.py"
    conftest_file.write_text(
        textwrap.dedent(
            f"""
            import pytest

            @pytest.fixture
            def shared_db_path():
                return {shared_db_path!r}
            """
        )
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider", "-q", str(worker_test_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"real `-n 2` xdist run against a fixed sqlite db_url failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
