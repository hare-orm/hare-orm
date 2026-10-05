import sys

import pytest

from hare.cli.output import terminal_colors as colors
from hare.cli.output.terminal_colors import TerminalColors


@pytest.fixture(autouse=True)
def _clean_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def test_supports_color_force_color_overrides_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """FORCE_COLOR is an explicit user override - it must win even when stdout isn't a real
    terminal (e.g. output piped to a file), not just tweak the auto-detected result."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert TerminalColors.supports_color() is True


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # FORCE_COLOR=0 is the documented way (force-color.org) to explicitly force color OFF.
        pytest.param("FORCE_COLOR", "0", id="supports_color_force_color_zero_disables"),
        # NO_COLOR (no-color.org) must win over auto-detection - presence alone opts out, even on a
        # real, color-capable terminal.
        pytest.param("NO_COLOR", "1", id="supports_color_no_color_disables_even_with_real_tty"),
        # no-color.org: "regardless of its value" - an empty NO_COLOR still counts as present.
        pytest.param("NO_COLOR", "", id="supports_color_no_color_empty_value_still_disables"),
    ],
)
def test_supports_color_disabled_by_the_environment(monkeypatch: pytest.MonkeyPatch, variable, value) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("WT_SESSION", "1")
    monkeypatch.setenv(variable, value)
    assert TerminalColors.supports_color() is False


class TestWindowsConsoleColorHeuristic:
    """A classic conhost.exe console (no WT_SESSION/ANSICON) used to unconditionally disable
    color on Windows - it should instead try enabling ANSI processing via SetConsoleMode
    (supported since Windows 10) before giving up."""

    def test_supports_color_windows_without_wt_session_tries_enabling_ansi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("WT_SESSION", raising=False)
        monkeypatch.delenv("ANSICON", raising=False)
        monkeypatch.setattr(colors.TerminalColors, "enable_windows_virtual_terminal_processing", lambda: True)
        assert TerminalColors.supports_color() is True

    def test_supports_color_windows_without_wt_session_falls_back_when_enabling_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("WT_SESSION", raising=False)
        monkeypatch.delenv("ANSICON", raising=False)
        monkeypatch.setattr(colors.TerminalColors, "enable_windows_virtual_terminal_processing", lambda: False)
        assert TerminalColors.supports_color() is False

    def test_supports_color_windows_with_wt_session_skips_enabling_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WT_SESSION already means ANSI works - no need to even try SetConsoleMode."""
        calls = []
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("WT_SESSION", "1")
        monkeypatch.setattr(
            colors.TerminalColors, "enable_windows_virtual_terminal_processing", lambda: calls.append(1) or False
        )
        assert TerminalColors.supports_color() is True
        assert calls == []

    def test_enable_windows_virtual_terminal_processing_sets_the_expected_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict = {}

        class FakeKernel32:
            def GetStdHandle(self, handle_id):
                calls["handle_id"] = handle_id
                return 123

            def GetConsoleMode(self, handle, mode_ptr):
                return 1

            def SetConsoleMode(self, handle, new_mode):
                calls["new_mode"] = new_mode
                return 1

        monkeypatch.setattr(colors.ctypes, "WinDLL", lambda name, use_last_error=True: FakeKernel32(), raising=False)

        assert colors.TerminalColors.enable_windows_virtual_terminal_processing() is True
        assert calls["handle_id"] == colors.STD_OUTPUT_HANDLE
        assert calls["new_mode"] & colors.ENABLE_VIRTUAL_TERMINAL_PROCESSING

    def test_enable_windows_virtual_terminal_processing_swallows_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SetConsoleMode failure (WinError, older Windows without the flag) must fall back to
        False, never raise out to the CLI."""

        class FailingKernel32:
            def GetStdHandle(self, handle_id):
                return 123

            def GetConsoleMode(self, handle, mode_ptr):
                return 1

            def SetConsoleMode(self, handle, new_mode):
                raise OSError("simulated WinError")

        monkeypatch.setattr(
            colors.ctypes, "WinDLL", lambda name, use_last_error=True: FailingKernel32(), raising=False
        )

        assert colors.TerminalColors.enable_windows_virtual_terminal_processing() is False

    def test_enable_windows_virtual_terminal_processing_returns_false_when_get_console_mode_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeKernel32:
            def GetStdHandle(self, handle_id):
                return 123

            def GetConsoleMode(self, handle, mode_ptr):
                return 0

            def SetConsoleMode(self, handle, new_mode):
                raise AssertionError("must not be called when GetConsoleMode fails")

        monkeypatch.setattr(colors.ctypes, "WinDLL", lambda name, use_last_error=True: FakeKernel32(), raising=False)

        assert colors.TerminalColors.enable_windows_virtual_terminal_processing() is False

    def test_enable_windows_virtual_terminal_processing_returns_false_when_windll_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `ctypes.WinDLL` at all (non-Windows, or an ancient Windows Python build) must not
        raise - just report failure so supports_color() falls back to no color."""
        monkeypatch.delattr(colors.ctypes, "WinDLL", raising=False)
        assert colors.TerminalColors.enable_windows_virtual_terminal_processing() is False
