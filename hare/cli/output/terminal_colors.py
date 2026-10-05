from __future__ import annotations

import ctypes
import os
import sys

from hare.cli.constants import ENABLE_VIRTUAL_TERMINAL_PROCESSING, STD_OUTPUT_HANDLE


class TerminalColors:
    """ANSI color codes for CLI output, empty when the terminal doesn't support them."""

    @staticmethod
    def enable_windows_virtual_terminal_processing() -> bool:
        """Turns on ANSI escape processing for the console (``SetConsoleMode``) - supported since
        Windows 10, off by default in conhost.

        Returns:
            Whether it succeeded - never raises.
        """
        try:
            # getattr(): ctypes.WinDLL exists only in the stub's win32 part.
            windll_class = getattr(ctypes, "WinDLL", None)
            if windll_class is None:
                return False
            kernel32 = windll_class("kernel32", use_last_error=True)
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
        except (AttributeError, OSError):
            return False

    @staticmethod
    def supports_color() -> bool:
        """Whether the terminal shows ANSI colors. FORCE_COLOR/NO_COLOR decide first, whatever the
        terminal.
        """
        if "FORCE_COLOR" in os.environ:
            return os.environ["FORCE_COLOR"] not in {"0", "false", "False"}
        if "NO_COLOR" in os.environ:
            # Presence alone opts out, regardless of value - an empty NO_COLOR still counts.
            return False
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return False
        # Read through a variable, so the type checker doesn't mark the other platform's branch
        # unreachable.
        platform = sys.platform
        if platform == "win32":
            # Windows Terminal and ANSICON enable ANSI themselves; conhost needs SetConsoleMode.
            if "WT_SESSION" in os.environ or "ANSICON" in os.environ:
                return True
            return TerminalColors.enable_windows_virtual_terminal_processing()
        return True

    #: Whether the codes below are set - decided once the class exists, as its methods are what
    #: decide it.
    COLOR = False

    # ANSI color codes
    BOLD = ""

    DIM = ""

    GREEN = ""

    YELLOW = ""

    CYAN = ""

    RED = ""

    RESET = ""

    @classmethod
    def use_colors(cls, color: bool) -> None:
        """Sets the color codes - empty without colors.

        Args:
            color: Whether the terminal shows colors.
        """
        cls.COLOR = color
        cls.BOLD = "\033[1m" if color else ""
        cls.DIM = "\033[2m" if color else ""
        cls.GREEN = "\033[32m" if color else ""
        cls.YELLOW = "\033[33m" if color else ""
        cls.CYAN = "\033[36m" if color else ""
        cls.RED = "\033[31m" if color else ""
        cls.RESET = "\033[0m" if color else ""


TerminalColors.use_colors(TerminalColors.supports_color())
