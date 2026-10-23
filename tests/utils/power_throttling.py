from __future__ import annotations

import ctypes
import sys

from tests.utils.constants import POWER_THROTTLING_CONTROLS, POWER_THROTTLING_INFORMATION_CLASS


class PowerThrottlingState(ctypes.Structure):
    """Windows' ``PROCESS_POWER_THROTTLING_STATE``."""

    _fields_ = [("version", ctypes.c_ulong), ("control_mask", ctypes.c_ulong), ("state_mask", ctypes.c_ulong)]


class PowerThrottling:
    """Windows' power throttling (EcoQoS) of the current process - of a test worker, of a benchmark run."""

    @staticmethod
    def turn_off() -> None:
        """Opts the process out of power throttling. Windows otherwise may run a process without a
        window in front on a hybrid processor's efficiency cores, at half the speed: parallel test workers
        crawl, and a benchmark run's numbers depend on where the scheduler put it. Nothing to do on other
        systems.
        """
        if sys.platform != "win32":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        state = PowerThrottlingState(1, POWER_THROTTLING_CONTROLS, 0)
        if not kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), POWER_THROTTLING_INFORMATION_CLASS, ctypes.byref(state), ctypes.sizeof(state)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
