from pathlib import Path

import pytest

from hare.core.config import HareConfig
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.api.plan import plan
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor import MigrationTarget

pytestmark = pytest.mark.sqlite_only


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_plan_unknown_app_label():
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        await plan(
            config=HareConfig.from_db_url(
                "sqlite+aiosqlite://:memory:", {"models": ["tests.shared_models.testmodels"]}
            ),
            app_labels=["ghost"],
        )


@pytest.mark.asyncio
async def test_plan_no_migrations_configured_returns_header_only():
    config = HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.unmigrated.models"]})
    output = await plan(config=config)
    assert [(group.connection_alias, group.steps) for group in output] == [("default", ())]


@pytest.mark.database_independent
def test_parse_targets_defaults_to_latest_for_every_app():
    targets = MigrationRequestParser.parse_targets(None, ["app_a", "app_b"])
    assert targets == [
        MigrationTarget(app_label="app_a", name="__latest__"),
        MigrationTarget(app_label="app_b", name="__latest__"),
    ]


@pytest.mark.parametrize(
    ("target", "expected_name"),
    [
        pytest.param("app_a.0002_second", "0002_second", id="parse_targets_dotted"),
        pytest.param("app_a", "__latest__", id="parse_targets_undotted_app_label_only"),
    ],
)
@pytest.mark.database_independent
def test_parse_targets(target, expected_name):
    targets = MigrationRequestParser.parse_targets(target, ["app_a", "app_b"])
    assert targets == [MigrationTarget(app_label="app_a", name=expected_name)]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("ghost.0001_initial", id="parse_targets_dotted_unknown_app_label"),
        pytest.param("ghost", id="parse_targets_undotted_unknown_app_label"),
    ],
)
@pytest.mark.database_independent
def test_parse_targets_unknown_app_label(target):
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        MigrationRequestParser.parse_targets(target, ["app_a"])


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
    output = await plan(config=config, target="planscopedapp")

    assert [
        (group.connection_alias, [(str(step.migration), step.backward) for step in group.steps]) for group in output
    ] == [("conn_a", [("planscopedapp.0001_initial", False)])]
