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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object lifecycle")
@pytest.mark.parametrize("cancel", [True, False], ids=["cancel-run", "root-exits-first"])
def test_windows_job_closes_descendants_even_after_the_root_exits(tmp_path, cancel):
    import win32api
    import win32con
    import win32event

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
            child = win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION, False, int(ready.read_text())
            )
            try:
                if cancel:
                    groups.stop()
                else:
                    release.touch()
                status = future.result(timeout=10)
                assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
                assert not groups.processes
                if not cancel:
                    assert status == 0
            finally:
                child.Close()
        finally:
            groups.stop()
