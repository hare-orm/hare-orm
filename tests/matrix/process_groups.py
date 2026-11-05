from __future__ import annotations

import os
import signal
import subprocess
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from tests.matrix.constants import PROCESS_STOP_TIMEOUT_SECONDS
from tests.matrix.messages import MATRIX_CLEANUP_FAILED

if TYPE_CHECKING:
    from tests.matrix.windows_process_group import WindowsProcessGroup


class ProcessGroups:
    """Track launched process groups and stop their descendants before joining worker threads.

    POSIX processes get a new session. Windows processes enter a Job Object before running.
    Only groups started by this instance are targeted. Marking the group stopped also
    prevents queued work from launching.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.processes: set[subprocess.Popen[Any] | WindowsProcessGroup] = set()
        self.stopped = False

    def run(self, command: list[str], *, cwd: Path, environment: dict[str, str], output: IO[str]) -> int:
        """Wait for one process, retaining its handle until it has finished."""
        with self.lock:
            if self.stopped:
                raise CancelledError
            process: subprocess.Popen[Any] | WindowsProcessGroup
            if os.name == "nt":
                from tests.matrix.windows_process_group import WindowsProcessGroup

                process = WindowsProcessGroup(command, cwd=cwd, environment=environment, output=output)
            else:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.processes.add(process)
        errors: list[BaseException] = []
        try:
            result = process.wait()
        except BaseException as error:
            errors.append(error)
            try:
                self.terminate(process)
                process.wait()
            except BaseException as cleanup_error:
                errors.append(cleanup_error)
        try:
            if not isinstance(process, subprocess.Popen):
                process.close()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except BaseException as error:
            errors.append(error)
        finally:
            with self.lock:
                self.processes.discard(process)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(MATRIX_CLEANUP_FAILED, errors)
        return result

    @staticmethod
    def terminate(process: subprocess.Popen[Any] | WindowsProcessGroup) -> None:
        """Stop a launched process tree and reap its root; repeated exits are harmless."""
        if not isinstance(process, subprocess.Popen):
            process.terminate()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def stop(self) -> None:
        """Prevent new launches and attempt to stop every active process tree."""
        with self.lock:
            self.stopped = True
            processes = tuple(self.processes)
        errors = []
        for process in processes:
            try:
                self.terminate(process)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup(MATRIX_CLEANUP_FAILED, errors)
