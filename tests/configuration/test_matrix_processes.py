from __future__ import annotations

import asyncio
import io
import os
import sys
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.matrix.process_groups import ProcessGroups
from tests.utils.waiting import wait_until

pytestmark = pytest.mark.database_independent


def test_stopped_process_group_refuses_queued_launches(tmp_path):
    groups = ProcessGroups()
    groups.stop()
    with pytest.raises(CancelledError):
        groups.run(
            [sys.executable, "-c", "raise AssertionError('must not start')"],
            cwd=tmp_path,
            environment={},
            output=io.StringIO(),
        )


def is_running(pid: int) -> bool:
    """Whether a process is alive - a zombie no init reaped (PID 1 of a container) counts as ended."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel-run", "root-exits-first"])
def test_the_session_ends_descendants_even_after_the_root_exits(tmp_path, cancel):
    groups = ProcessGroups()
    ready, release = tmp_path / "ready", tmp_path / "release"
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
        f"Path({str(ready)!r}).write_text(str(child.pid)); "
        f"release=Path({str(release)!r}); "
        "\nwhile not release.exists(): time.sleep(0.01)\n"
    )
    with (tmp_path / "run.log").open("w") as output, ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            groups.run, [sys.executable, "-c", code], cwd=Path.cwd(), environment=dict(os.environ), output=output
        )
        try:
            asyncio.run(wait_until(ready.exists))
            child_pid = int(ready.read_text())
            assert is_running(child_pid)
            if cancel:
                groups.stop()
            else:
                release.touch()
            status = future.result(timeout=10)
            asyncio.run(wait_until(lambda: not is_running(child_pid)))
            assert not groups.processes
            if not cancel:
                assert status == 0
        finally:
            groups.stop()
