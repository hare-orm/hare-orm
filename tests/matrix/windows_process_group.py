from __future__ import annotations

import msvcrt
import os
import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any

import win32api
import win32con
import win32event
import win32job
import win32process

from tests.matrix.messages import WINDOWS_STARTUP_CLEANUP_FAILED


class WindowsProcessGroup:
    """A suspended process assigned to a kill-on-close Job Object before it can spawn workers.

    pywin32 supplies the Windows structures and API bindings. Inherited stdout/stdin handles
    are duplicated for startup and closed in the parent immediately afterwards. Closing the
    job ends descendants even when their root process has already exited.
    """

    def __init__(self, command: list[str], *, cwd: Path, environment: dict[str, str], output: IO[str]) -> None:
        self.lock = threading.Lock()
        self.job = win32job.CreateJobObject(None, "")
        self.process: Any = None
        try:
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            with ExitStack() as cleanup:
                input_file = cleanup.enter_context(open(os.devnull))
                startup = win32process.STARTUPINFO()
                startup.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
                startup.wShowWindow = win32con.SW_HIDE
                startup.hStdInput = self.duplicate_handle(input_file)
                cleanup.callback(startup.hStdInput.Close)
                startup.hStdOutput = self.duplicate_handle(output)
                cleanup.callback(startup.hStdOutput.Close)
                startup.hStdError = startup.hStdOutput
                self.process, thread, _, _ = win32process.CreateProcess(
                    None,
                    subprocess.list2cmdline(command),
                    None,
                    None,
                    True,
                    win32con.CREATE_SUSPENDED | win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_NO_WINDOW,
                    environment,
                    str(cwd),
                    startup,
                )
                cleanup.callback(thread.Close)
                win32job.AssignProcessToJobObject(self.job, self.process)
                win32process.ResumeThread(thread)
        except BaseException as error:
            failures: list[BaseException] = [error]
            if self.process is not None:
                try:
                    win32api.TerminateProcess(self.process, 1)
                    self.wait()
                except BaseException as cleanup_error:
                    failures.append(cleanup_error)
            try:
                self.close()
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
            if len(failures) > 1:
                raise BaseExceptionGroup(WINDOWS_STARTUP_CLEANUP_FAILED, failures) from None
            raise

    @staticmethod
    def duplicate_handle(file: IO[str]) -> Any:
        process = win32api.GetCurrentProcess()
        return win32api.DuplicateHandle(
            process,
            msvcrt.get_osfhandle(file.fileno()),
            process,
            0,
            True,
            win32con.DUPLICATE_SAME_ACCESS,
        )

    def wait(self) -> int:
        win32event.WaitForSingleObject(self.process, win32event.INFINITE)
        return win32process.GetExitCodeProcess(self.process)

    def terminate(self) -> None:
        """Close the job once; the waiting execution thread retains the root process handle."""
        with self.lock:
            job, self.job = self.job, None
        if job is not None:
            job.Close()

    def close(self) -> None:
        """Release the job and root handle after wait has finished."""
        try:
            self.terminate()
        finally:
            process, self.process = self.process, None
            if process is not None:
                process.Close()
