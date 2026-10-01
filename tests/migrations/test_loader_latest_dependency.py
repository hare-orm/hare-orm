from pathlib import Path

import pytest

from hare.exceptions import ConfigurationError
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.recorder import NoopRecorder


def _write_migration(migrations_dir: Path, name: str, dependencies: list[tuple[str, str]]) -> None:
    (migrations_dir / f"{name}.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "",
                "class Migration(migrations.Migration):",
                f"    dependencies = {dependencies!r}",
                "    operations = []",
                "",
            ]
        ),
        encoding="ascii",
    )


def _write_app(tmp_path: Path, app_label: str) -> tuple[Path, str]:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return migrations_dir, f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_cross_app_latest_dependency_raises_on_an_unresolved_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """appb's own migration history has forked into two heads with no merge migration -
    graph.get_single_leaf("latest_dep_appb") already raises ConfigurationError for that fork on its own.
    appa declares `dependencies = [("latest_dep_appb", "__latest__")]` - build_graph() used to resolve this
    via `leaf_nodes(app_label)[0]` directly (bypassing get_single_leaf()'s ambiguity check
    entirely), silently binding to whichever branch sorted first by name instead of raising the
    same ambiguity error a caller would get for appb's own graph."""
    appb_dir, appb_module = _write_app(tmp_path, "latest_dep_appb")
    _write_migration(appb_dir, "0001_initial", [])
    _write_migration(appb_dir, "0002_a", [("latest_dep_appb", "0001_initial")])
    _write_migration(appb_dir, "0002_b", [("latest_dep_appb", "0001_initial")])

    appa_dir, appa_module = _write_app(tmp_path, "latest_dep_appa")
    _write_migration(appa_dir, "0001_initial", [("latest_dep_appb", "__latest__")])

    monkeypatch.syspath_prepend(str(tmp_path))

    loader = MigrationLoader(
        apps_config={
            "latest_dep_appa": {"migrations": appa_module},
            "latest_dep_appb": {"migrations": appb_module},
        },
        recorder=NoopRecorder(),
    )

    with pytest.raises(ConfigurationError, match="Conflicting migrations"):
        await loader.build_graph()
