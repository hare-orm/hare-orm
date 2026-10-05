from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from hare import fields
from hare.core.hare_context import HareContext
from hare.fields.field import Field
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.operations import AddField, CreateModel, DeleteModel, RemoveField, RenameField, RenameModel
from hare.migrations.state.state_apps import StateApps
from hare.models import Model


def _prepare_migration_package(tmp_path: Path, app_label: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return f"{app_label}.migrations"


def _write_migration(
    tmp_path: Path,
    app_label: str,
    name: str,
    dependencies: list[tuple[str, str]] | None = None,
) -> str:
    module_path = _prepare_migration_package(tmp_path, app_label)
    migrations_dir = tmp_path / app_label / "migrations"
    content = [
        "from hare import migrations",
        "",
        "class Migration(migrations.Migration):",
        f"    dependencies = {dependencies or []!r}",
        "",
        "    operations = []",
        "",
    ]
    (migrations_dir / f"{name}.py").write_text("\n".join(content), encoding="ascii")
    return module_path


def _write_migration_with_ops(
    tmp_path: Path,
    app_label: str,
    name: str,
    operations_source: list[str],
    dependencies: list[tuple[str, str]] | None = None,
) -> str:
    module_path = _prepare_migration_package(tmp_path, app_label)
    migrations_dir = tmp_path / app_label / "migrations"
    content = [
        "from hare import migrations",
        "from hare.migrations import operations as ops",
        "from hare import fields",
        "",
        "class Migration(migrations.Migration):",
        f"    dependencies = {dependencies or []!r}",
        "",
        "    operations = [",
        *operations_source,
        "    ]",
        "",
    ]
    (migrations_dir / f"{name}.py").write_text("\n".join(content), encoding="ascii")
    return module_path


def _make_model(name: str, app_label: str, managed: bool | None = None, **model_fields: Field) -> type[Model]:
    attrs: dict[str, Any] = dict(model_fields)
    meta_attrs: dict[str, Any] = {"app": app_label, "table": name.lower()}
    if managed is not None:
        meta_attrs["managed"] = managed
    meta = type("Meta", (), meta_attrs)
    attrs["Meta"] = meta
    return type(name, (Model,), attrs)


@pytest.mark.asyncio
async def test_autodetector_initial_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _prepare_migration_package(tmp_path, "autoapp_init")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model("Widget", "autoapp_init", id=fields.IntField(primary_key=True))
    apps.register_model("autoapp_init", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "autoapp_init": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    writer = changes[0]
    assert writer.name == "0001_initial"
    assert writer.initial is True
    assert any(isinstance(op, CreateModel) for op in writer.operations)


@pytest.mark.asyncio
async def test_autodetector_uses_latest_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migration(tmp_path, "autoapp_dep", "0001_initial")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model("Widget", "autoapp_dep", id=fields.IntField(primary_key=True))
    apps.register_model("autoapp_dep", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "autoapp_dep": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    writer = changes[0]
    assert writer.dependencies == [("autoapp_dep", "0001_initial")]
    assert writer.name == "0002_auto_20240101_1200"
    assert writer.initial is False


@pytest.mark.asyncio
async def test_autodetector_adds_relation_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app1_module = _write_migration(tmp_path, "autoapp1", "0001_initial")
    app2_module = _write_migration(tmp_path, "autoapp2", "0003_latest")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Team = _make_model("Team", "autoapp2", id=fields.IntField(primary_key=True))
    Widget = _make_model(
        "Widget",
        "autoapp1",
        id=fields.IntField(primary_key=True),
        team=fields.ForeignKeyField("autoapp2.Team"),
    )
    apps.register_model("autoapp2", Team)
    apps.register_model("autoapp1", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "autoapp1": {"models": [], "default_connection": "default", "migrations": app1_module},
            "autoapp2": {"models": [], "default_connection": "default", "migrations": app2_module},
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 2
    writer1 = next(writer for writer in changes if writer.app_label == "autoapp1")
    # Team doesn't exist yet in EITHER app's on-disk state (both "0001_initial"/"0003_latest" are
    # empty stubs) - autoapp2 gets its own brand-new migration in this same batch that actually
    # creates Team, so THAT is the real dependency, not the stale on-disk leaf "0003_latest"
    # (which creates nothing at all - depending on it alone would let the executor apply
    # autoapp1's Widget-with-FK-to-Team migration before Team's table exists).
    assert ("autoapp2", "0004_auto_20240101_1200") in writer1.dependencies


@pytest.mark.asyncio
async def test_autodetector_multi_leaf_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migration(tmp_path, "branchapp", "0001_alpha")
    _write_migration(tmp_path, "branchapp", "0002_beta")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model("Widget", "branchapp", id=fields.IntField(primary_key=True))
    apps.register_model("branchapp", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "branchapp": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    writer = changes[0]
    assert set(writer.dependencies) == {
        ("branchapp", "0001_alpha"),
        ("branchapp", "0002_beta"),
    }


@pytest.mark.asyncio
async def test_autodetector_model_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migration_with_ops(
        tmp_path,
        "renameapp",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='OldWidget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "                ('name', fields.CharField(max_length=100)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    attrs = {
        "id": fields.IntField(primary_key=True),
        "name": fields.CharField(max_length=100),
    }
    meta = type("Meta", (), {"app": "renameapp", "table": "newwidget"})
    attrs["Meta"] = meta
    NewWidget = type("NewWidget", (Model,), attrs)
    apps.register_model("renameapp", NewWidget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "renameapp": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    ops = changes[0].operations
    assert any(isinstance(op, RenameModel) for op in ops)


@pytest.mark.asyncio
async def test_autodetector_field_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migration_with_ops(
        tmp_path,
        "renamefield",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Widget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "                ('title', fields.CharField(max_length=100)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    attrs = {
        "id": fields.IntField(primary_key=True),
        "name": fields.CharField(max_length=100, source_field="title"),
    }
    meta = type("Meta", (), {"app": "renamefield", "table": "widget"})
    attrs["Meta"] = meta
    Widget = type("Widget", (Model,), attrs)
    apps.register_model("renamefield", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "renamefield": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    ops = changes[0].operations
    assert any(isinstance(op, RenameField) for op in ops)


@pytest.mark.asyncio
async def test_autodetector_model_rename_combined_with_field_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a model AND one of its fields in the same change used to defeat rename detection
    entirely: OperationGenerator._match_renamed_models() compared old/new model_signature()s as a
    dict keyed by field NAME, so a field-name change alone made the two signatures compare
    unequal even though every field's actual content was identical - the model rename fell
    through to a plain CreateModel+DeleteModel pair (a real, complete DROP TABLE on migrate),
    instead of the intended RenameModel+RenameField. This is an entirely ordinary refactor
    (rename a model, tidy up one of its field names at the same time), not an exotic edge case."""
    module_path = _write_migration_with_ops(
        tmp_path,
        "renameboth",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='OldWidget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "                ('title', fields.CharField(max_length=100)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    attrs = {
        "id": fields.IntField(primary_key=True),
        "name": fields.CharField(max_length=100, source_field="title"),
    }
    meta = type("Meta", (), {"app": "renameboth", "table": "newwidget"})
    attrs["Meta"] = meta
    NewWidget = type("NewWidget", (Model,), attrs)
    apps.register_model("renameboth", NewWidget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "renameboth": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    ops = changes[0].operations
    assert any(isinstance(op, RenameModel) for op in ops), ops
    assert any(isinstance(op, RenameField) for op in ops), ops
    assert not any(isinstance(op, (CreateModel, DeleteModel)) for op in ops), ops


@pytest.mark.asyncio
async def test_autodetector_model_rename_combined_with_new_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a model AND adding a new field to it in the same change used to defeat rename
    detection entirely, the same way as the field-rename case above but one dimension over:
    _match_renamed_models()'s exact-signature match requires the field-content MULTISET to be
    identical, and adding a field changes that multiset's own size - so an entirely ordinary
    refactor ("renamed the model, added a field while I was in there") fell through to a plain
    CreateModel+DeleteModel pair (a real, complete DROP TABLE + CREATE TABLE on migrate, silent
    total data loss), instead of the intended RenameModel+AddField."""
    module_path = _write_migration_with_ops(
        tmp_path,
        "renamenewfield",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='OldWidget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "                ('title', fields.CharField(max_length=100)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    attrs = {
        "id": fields.IntField(primary_key=True),
        "title": fields.CharField(max_length=100),
        "description": fields.CharField(max_length=200, null=True),
    }
    meta = type("Meta", (), {"app": "renamenewfield", "table": "newwidget"})
    attrs["Meta"] = meta
    NewWidget = type("NewWidget", (Model,), attrs)
    apps.register_model("renamenewfield", NewWidget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "renamenewfield": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    ops = changes[0].operations
    assert any(isinstance(op, RenameModel) for op in ops), ops
    assert any(isinstance(op, AddField) and op.name == "description" for op in ops), ops
    assert not any(isinstance(op, (CreateModel, DeleteModel)) for op in ops), ops


@pytest.mark.asyncio
async def test_autodetector_ambiguous_field_rename_falls_back_to_remove_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two removed fields sharing the same type/nullable "rename signature" as one added field
    is genuinely ambiguous - autodetection must not guess which one to pair up as a rename
    (RenameField's RENAME COLUMN would silently preserve the WRONG field's data under the new
    name), it must fall back to a plain remove+add for all of them."""
    module_path = _write_migration_with_ops(
        tmp_path,
        "ambiguousrename",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Widget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "                ('email', fields.IntField(null=True)),",
            "                ('fax', fields.IntField(null=True)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model(
        "Widget",
        "ambiguousrename",
        id=fields.IntField(primary_key=True),
        phone=fields.IntField(null=True),
    )
    apps.register_model("ambiguousrename", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "ambiguousrename": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    ops = changes[0].operations
    assert not any(isinstance(op, RenameField) for op in ops)
    removed_names = {op.name for op in ops if isinstance(op, RemoveField)}
    added_names = {op.name for op in ops if isinstance(op, AddField)}
    assert removed_names == {"email", "fax"}
    assert added_names == {"phone"}


@pytest.mark.asyncio
async def test_autodetector_skips_unmigrated_relation_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = _write_migration(tmp_path, "relapp", "0001_initial")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Team = _make_model("Team", "nomigs", id=fields.IntField(primary_key=True))
    Widget = _make_model(
        "Widget",
        "relapp",
        id=fields.IntField(primary_key=True),
        team=fields.ForeignKeyField("nomigs.Team"),
    )
    apps.register_model("nomigs", Team)
    apps.register_model("relapp", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "relapp": {"models": [], "default_connection": "default", "migrations": module_path},
            "nomigs": {"models": [], "default_connection": "default", "migrations": None},
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    writer = changes[0]
    assert ("nomigs", "0001_initial") not in writer.dependencies


@pytest.mark.asyncio
async def test_autodetector_relation_dependency_model_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app1_module = _write_migration(tmp_path, "classrel1", "0001_initial")
    app2_module = _write_migration(tmp_path, "classrel2", "0002_latest")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Team = _make_model("Team", "classrel2", id=fields.IntField(primary_key=True))
    Widget = _make_model(
        "Widget",
        "classrel1",
        id=fields.IntField(primary_key=True),
        team=fields.ForeignKeyField(Team),
    )
    apps.register_model("classrel2", Team)
    apps.register_model("classrel1", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "classrel1": {
                "models": [],
                "default_connection": "default",
                "migrations": app1_module,
            },
            "classrel2": {
                "models": [],
                "default_connection": "default",
                "migrations": app2_module,
            },
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    writer = next(writer for writer in changes if writer.app_label == "classrel1")
    # Same reasoning as test_autodetector_adds_relation_dependency above: Team doesn't exist in
    # classrel2's on-disk state either ("0002_latest" is an empty stub) - classrel2 gets its own
    # new migration in this batch that actually creates Team, so that's the real dependency.
    assert ("classrel2", "0003_auto_20240101_1200") in writer.dependencies


@pytest.mark.asyncio
async def test_autodetector_no_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migration_with_ops(
        tmp_path,
        "nochange",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Widget',",
            "            fields=[",
            "                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model("Widget", "nochange", id=fields.IntField(primary_key=True))
    apps.register_model("nochange", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "nochange": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert changes == []


@pytest.mark.asyncio
async def test_autodetector_skips_unmanaged_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Meta.managed = False must make a model completely invisible to makemigrations - no
    CreateModel, no migration file at all - the same defense-in-depth guarantee a runtime-
    registered "live" model (Hare.register_live_model()) relies on to never have hare-orm try to
    migrate a table it doesn't own."""
    module_path = _prepare_migration_package(tmp_path, "unmanagedapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Widget = _make_model("Widget", "unmanagedapp", managed=False, id=fields.IntField(primary_key=True))
    apps.register_model("unmanagedapp", Widget)

    autodetector = MigrationAutodetector(
        apps,
        {
            "unmanagedapp": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert changes == []


@pytest.mark.asyncio
async def test_autodetector_managed_model_unaffected_by_sibling_unmanaged_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed=True (or unset, defaulting True) model must still be picked up normally even
    when another model in the same app is managed=False - managed=False must exclude only that
    one model, not turn the whole app invisible to makemigrations."""
    module_path = _prepare_migration_package(tmp_path, "mixedmanagedapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    apps = StateApps()
    Managed = _make_model("Managed", "mixedmanagedapp", id=fields.IntField(primary_key=True))
    Unmanaged = _make_model("Unmanaged", "mixedmanagedapp", managed=False, id=fields.IntField(primary_key=True))
    apps.register_model("mixedmanagedapp", Managed)
    apps.register_model("mixedmanagedapp", Unmanaged)

    autodetector = MigrationAutodetector(
        apps,
        {
            "mixedmanagedapp": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        },
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    writer = changes[0]
    assert writer.name == "0001_initial"
    ops = writer.operations
    assert any(isinstance(op, CreateModel) and op.name == "Managed" for op in ops)
    assert not any(isinstance(op, CreateModel) and op.name == "Unmanaged" for op in ops)


@pytest.mark.asyncio
async def test_autodetector_rerun_fk_to_unmanaged_model_no_related_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the autodetector twice in the same process against a FK that targets a
    Meta.managed=False model, with no explicit related_name, used to crash the second run with
    ConfigurationError("backward relation ... duplicates"): StateApps._reference_is_loadable()
    registered the SAME live model class into every State it built for the FK target, so the
    default backward relation (e.g. "widgets") the first run's init_relations() added onto that
    live class was still there on the second run, colliding with the identical relation
    init_relations() was about to add again. StateApps._clone_unmanaged_model() now clones a
    fresh state-only class per State instead of reusing the live one, so this must not crash and
    must keep detecting the same changes on every run."""
    module_path = _write_migration_with_ops(
        tmp_path,
        "fkbackwardapp",
        "0001_initial",
        [
            "        ops.CreateModel(",
            "            name='Widget',",
            "            fields=[",
            "                ('id', fields.IntField(primary_key=True)),",
            "                ('external', fields.ForeignKeyField('fkbackwardapp.ExternalThing', db_index=True)),",
            "            ],",
            "        ),",
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "fkbackward.sqlite3")},
                }
            }
        )

        class ExternalThing(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                app = "fkbackwardapp"
                table = "external_thing"
                managed = False

        from hare.core.hare import Hare

        Hare.register_live_models([ExternalThing], app_label="fkbackwardapp", connection_alias="default")

        Widget = _make_model(
            "Widget",
            "fkbackwardapp",
            id=fields.IntField(primary_key=True),
            external=fields.ForeignKeyField("fkbackwardapp.ExternalThing"),
        )

        apps_config = {
            "fkbackwardapp": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        }

        for _ in range(2):
            apps = StateApps()
            apps.register_model("fkbackwardapp", Widget)
            autodetector = MigrationAutodetector(
                apps,
                apps_config,
                now=lambda: dt.datetime(2024, 1, 1, 12, 0),
            )
            changes = await autodetector.changes()
            assert changes == []
