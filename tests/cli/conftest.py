import sys

import pytest

from hare.cli.plugins.cli_command_registry import CLICommandRegistry


@pytest.fixture(scope="session", autouse=True)
def initialize_tests():
    return None


@pytest.fixture(scope="session")
def _installed_cli_entry_points():
    return CLICommandRegistry.get_entry_points()


@pytest.fixture(autouse=True)
def _read_cli_entry_points_once(monkeypatch: pytest.MonkeyPatch, _installed_cli_entry_points):
    """The installed packages' CLI entry points are read once per test process, not on every CLI
    run of a test - reading them takes ~20 ms, and no package is installed while the tests run."""
    monkeypatch.setattr(
        CLICommandRegistry, "get_entry_points", staticmethod(lambda: list(_installed_cli_entry_points))
    )


def _purge_dynamic_cli_packages() -> None:
    for name in [name for name in sys.modules if name == "cli_app" or name.startswith("cli_")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _isolate_dynamic_cli_packages():
    """Tests across this directory repeatedly create same-named throwaway packages (cli_app,
    cli_accounts, cli_forked, ...) under a fresh tmp_path and import them by name. Without this,
    a stale sys.modules entry left by an earlier test in the same worker process can shadow a
    later test's own tmp_path package (wrong __path__, missing submodules) - individual tests
    have inconsistently patched around this with ad-hoc sys.modules.pop() calls, which is easy to
    forget and was causing intermittent failures depending on test order/worker assignment."""
    _purge_dynamic_cli_packages()
    yield
    _purge_dynamic_cli_packages()
