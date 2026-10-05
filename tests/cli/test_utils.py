import importlib
from pathlib import Path
from types import ModuleType

import pytest

from hare.cli.config_locator import ConfigLocator
from hare.core.config import HareConfig
from hare.exceptions import ConfigurationError
from hare.migrations.loading.migrations_modules import MigrationsModules

EMPTY_HARE_ORM = None


def test_hare_orm_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARE_ORM", "app.settings.HARE_ORM")
    assert ConfigLocator.locate() == "app.settings.HARE_ORM"


def test_hare_orm_config_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.hare]
hare_orm = "settings.HARE_ORM"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARE_ORM", raising=False)
    assert ConfigLocator.locate() == "settings.HARE_ORM"


def test_hare_orm_config_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARE_ORM", raising=False)
    assert ConfigLocator.locate() == ""


def test_get_hare_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_name = f"cli_settings_{tmp_path.name}"
    settings = tmp_path / f"{module_name}.py"
    settings.write_text(
        "HARE_ORM = {\n"
        "    'connections': {'default': 'sqlite+aiosqlite://:memory:'},\n"
        "    'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    import sys

    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    result = HareConfig.load(f"{module_name}.HARE_ORM")
    assert isinstance(result, HareConfig)
    config_dict = result.to_dict()
    assert config_dict["connections"] == {"default": "sqlite+aiosqlite://:memory:"}
    assert config_dict["apps"]["models"]["models"] == ["__main__"]

    with pytest.raises(
        ConfigurationError,
        match="Cannot import configuration module 'missing'",
    ):
        HareConfig.load("missing.HARE_ORM")

    with pytest.raises(
        ConfigurationError,
        match="Variable 'MISSING' not found in module",
    ):
        HareConfig.load(f"{module_name}.MISSING")


def test_a_config_module_importing_a_missing_package_names_that_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"broken_settings_{tmp_path.name}"
    (tmp_path / f"{module_name}.py").write_text("import hare_no_such_package\nHARE_ORM = {}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(ConfigurationError, match="failed to import: No module named 'hare_no_such_package'"):
        HareConfig.load(f"{module_name}.HARE_ORM")
    with pytest.raises(ConfigurationError, match="Cannot import configuration module 'missing_package.settings'"):
        HareConfig.load("missing_package.settings.HARE_ORM")


def test_infer_migrations_module() -> None:
    module = ModuleType("demo.models")
    assert MigrationsModules.infer([module]) == "demo.migrations"
    assert MigrationsModules.infer(["demo.models"]) == "demo.migrations"
    assert MigrationsModules.infer(["demo.sub.models"]) == "demo.sub.migrations"
    assert MigrationsModules.infer(["demo.other"]) == "demo.migrations"
    assert MigrationsModules.infer(None) is None


def test_normalize_apps_config_infers_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_dir = tmp_path / "cli_app"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "models.py").write_text("", encoding="utf-8")
    migrations_dir = app_dir / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "__init__.py").write_text("", encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    import sys

    sys.modules.pop("cli_app_no_migrations", None)
    sys.modules.pop("cli_app_no_migrations.migrations", None)
    apps = {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
        }
    }
    normalized = MigrationsModules.get_apps_with_existing_modules(apps)
    assert normalized["app"]["migrations"] == "cli_app.migrations"


def test_normalize_apps_config_skips_missing_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_dir = tmp_path / "cli_app_no_migrations"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "models.py").write_text("", encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    apps = {
        "app": {
            "models": ["cli_app_no_migrations.models"],
            "default_connection": "default",
        }
    }
    normalized = MigrationsModules.get_apps_with_existing_modules(apps)
    assert "migrations" not in normalized["app"]
