from pathlib import Path

import pytest

from hare import Hare
from hare.core.config import AppConfig, ConnectionConfig, HareConfig
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.api.plan import plan
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor import MigrationTarget, PlanStep
from hare.migrations.migration import Migration


def _sqlite_config(**app_kwargs) -> HareConfig:
    return HareConfig(
        connections={
            "default": ConnectionConfig(
                engine="sqlite+aiosqlite",
                credentials={"file_path": ":memory:"},
            )
        },
        apps={"models": AppConfig(models=["tests.testmodels"], default_connection="default", **app_kwargs)},
    )


@pytest.mark.asyncio
async def test_plan_unknown_app_label():
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        await plan(config=_sqlite_config(), app_labels=["ghost"])


@pytest.mark.asyncio
async def test_plan_no_migrations_configured_returns_header_only():
    config = HareConfig(
        connections=_sqlite_config().connections,
        apps={"models": AppConfig(models=["tests.unmigrated.models"], default_connection="default")},
    )
    try:
        output = await plan(config=config)
    finally:
        await Hare.close_connections()
    assert output == ["# Connection: default"]


def test_parse_targets_defaults_to_latest_for_every_app():
    targets = MigrationRequestParser.parse_targets(None, ["app_a", "app_b"])
    assert targets == [
        MigrationTarget(app_label="app_a", name="__latest__"),
        MigrationTarget(app_label="app_b", name="__latest__"),
    ]


def test_parse_targets_dotted():
    targets = MigrationRequestParser.parse_targets("app_a.0002_second", ["app_a", "app_b"])
    assert targets == [MigrationTarget(app_label="app_a", name="0002_second")]


def test_parse_targets_dotted_unknown_app_label():
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        MigrationRequestParser.parse_targets("ghost.0001_initial", ["app_a"])


def test_parse_targets_undotted_app_label_only():
    targets = MigrationRequestParser.parse_targets("app_a", ["app_a", "app_b"])
    assert targets == [MigrationTarget(app_label="app_a", name="__latest__")]


def test_parse_targets_undotted_unknown_app_label():
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        MigrationRequestParser.parse_targets("ghost", ["app_a"])


def _write_empty_initial_migration(tmp_path: Path, app_label: str) -> str:
    migrations_dir = tmp_path / app_label / "migrations"
    migrations_dir.mkdir(parents=True)
    (tmp_path / app_label / "__init__.py").write_text("", encoding="ascii")
    (tmp_path / app_label / "models.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "0001_initial.py").write_text(
        "from hare import migrations\n\nclass Migration(migrations.Migration):\n    operations = []\n",
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_plan_with_scoped_target_does_not_report_other_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same scoping as migrate(): an explicit target on one connection must not make plan()
    report every other connection's apps as pending."""
    planned_module = _write_empty_initial_migration(tmp_path, "planscopedapp")
    other_module = _write_empty_initial_migration(tmp_path, "planotherconnapp")
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {
        "connections": {
            "conn_a": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / "a.sqlite3")},
            },
            "conn_b": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / "b.sqlite3")},
            },
        },
        "apps": {
            "planscopedapp": {
                "models": ["planscopedapp.models"],
                "default_connection": "conn_a",
                "migrations": planned_module,
            },
            "planotherconnapp": {
                "models": ["planotherconnapp.models"],
                "default_connection": "conn_b",
                "migrations": other_module,
            },
        },
    }
    try:
        output = await plan(config=config, target="planscopedapp")
    finally:
        await Hare.close_connections()

    assert output == ["# Connection: conn_a", "+ planscopedapp.0001_initial"]


def test_format_steps_forward_and_backward():
    forward = PlanStep(migration=Migration("0001_initial", "app_a"), backward=False)
    backward = PlanStep(migration=Migration("0002_second", "app_a"), backward=True)
    lines = PlanStep.format_steps([forward, backward], "default")
    assert lines == [
        "# Connection: default",
        "+ app_a.0001_initial",
        "- app_a.0002_second",
    ]
