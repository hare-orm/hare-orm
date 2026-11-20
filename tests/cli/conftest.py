import pytest

from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from tests.utils.project_imports import ProjectImports


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


@pytest.fixture(autouse=True)
def _isolate_dynamic_cli_packages(tmp_path):
    """Restore namespaces declared by this test's files without clearing unrelated cli_* modules."""
    with ProjectImports(tmp_path).isolate_modules():
        yield
