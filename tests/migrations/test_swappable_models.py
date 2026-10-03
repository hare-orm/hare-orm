"""Swappable models: a package's model a project replaces with its own through a ``swappable``
config setting, and relations declared with ``swappable()`` that follow the setting - on the live
registry, in migration files and against a real database."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from hare import Hare
from hare.contrib.test.helpers import truncate_all_models
from hare.core.config import HareConfig
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError
from hare.inspectdb.introspector import SchemaIntrospector
from hare.migrations import SwappableDependency, swappable_dependency
from hare.migrations.api.migration_maker import MigrationMaker
from hare.migrations.autodetection.autodetector import MigrationAutodetector
from hare.migrations.constants import FIRST_MIGRATION, ZERO_MIGRATION
from hare.migrations.drift import detect_drift_for_alias
from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.execution.executor import MigrationExecutor, MigrationTarget
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.recorder import NoopRecorder
from hare.migrations.writer import MigrationWriter
from hare.models import swappable
from tests.utils.database_under_test import DatabaseUnderTest

PACKAGE_APP = "pkg"
PROJECT_APP = "proj"
SETTING = "MEMBER_MODEL"
CONNECTION = "default"

PACKAGE_MODELS_SOURCE = """
from hare import fields
from hare.models import Model, swappable


class BaseMember(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        abstract = True


class Member(BaseMember):
    class Meta:
        swappable = "MEMBER_MODEL"


class Consent(Model):
    id = fields.IntField(primary_key=True)
    member = fields.ForeignKeyField(swappable("MEMBER_MODEL"), related_name="consents", null=True)
    owner = fields.OneToOneField(swappable("MEMBER_MODEL"), related_name="owned_consent", null=True)
    witnesses = fields.ManyToManyField(swappable("MEMBER_MODEL"), related_name="witnessed_consents")
"""

PROJECT_MODELS_SOURCE = """
from hare import fields
from {package}.models import BaseMember


class Account(BaseMember):
    email = fields.CharField(max_length=100, default="")
"""

#: A project model pointing back at the package - with the package's relation to the project's
#: swapped-in model, both apps migrated together form a dependency cycle.
CYCLIC_PROJECT_MODELS_SOURCE = """
from hare import fields
from {package}.models import BaseMember


class Account(BaseMember):
    favorite_consent = fields.ForeignKeyField(
        "pkg.Consent", related_name="favorite_of", null=True, on_delete=fields.SET_NULL
    )
"""


class SwappableProject:
    """Model and migration packages written to disk, one fresh package name per use - model
    classes are process-wide, so each database/setting combination imports its own copy."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.package_migration_files: list[Path] = []

    def write_package(self, *, with_migrations: bool = True) -> str:
        package = f"swappable_pkg_{uuid.uuid4().hex[:10]}"
        package_dir = self.root / package
        (package_dir / "migrations").mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "models.py").write_text(PACKAGE_MODELS_SOURCE, encoding="utf-8")
        (package_dir / "migrations" / "__init__.py").write_text("", encoding="utf-8")
        if with_migrations:
            for migration_file in self.package_migration_files:
                shutil.copy(migration_file, package_dir / "migrations" / migration_file.name)
        return package

    def write_project(self, package: str, source: str = PROJECT_MODELS_SOURCE) -> str:
        project = f"swappable_proj_{uuid.uuid4().hex[:10]}"
        project_dir = self.root / project
        (project_dir / "migrations").mkdir(parents=True)
        (project_dir / "__init__.py").write_text("", encoding="utf-8")
        (project_dir / "models.py").write_text(source.format(package=package), encoding="utf-8")
        (project_dir / "migrations" / "__init__.py").write_text("", encoding="utf-8")
        return project

    @staticmethod
    def get_apps_config(package: str, project: str | None = None) -> dict[str, dict[str, Any]]:
        apps_config = {
            PACKAGE_APP: {
                "models": [f"{package}.models"],
                "default_connection": CONNECTION,
                "migrations": f"{package}.migrations",
            }
        }
        if project is not None:
            apps_config[PROJECT_APP] = {
                "models": [f"{project}.models"],
                "default_connection": CONNECTION,
                "migrations": f"{project}.migrations",
            }
        return apps_config


def get_connection_config(tmp_path: Path) -> Any:
    """One concrete test database's connection config - a file for SQLite, so several contexts
    can share it."""
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url or DatabaseUnderTest.is_file_database(db_url):
        scheme = (db_url or DatabaseUnderTest.DEFAULT_URL).split("://", 1)[0]
        db_url = f"{scheme}://{(tmp_path / 'swappable.sqlite3').as_posix()}"
    return DbUrlConfigGenerator.expand(db_url, testing=True)


def build_config(
    connection_config: Any, apps_config: dict[str, dict[str, Any]], swappable_settings: dict[str, str] | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {"connections": {CONNECTION: connection_config}, "apps": apps_config}
    if swappable_settings is not None:
        config["swappable"] = swappable_settings
    return config


@asynccontextmanager
async def open_context(
    config: dict[str, Any], *, create_db: bool = False, drop_db: bool = False, connect: bool = True
) -> AsyncGenerator[HareContext]:
    context = HareContext()
    async with context:
        await context.init(config=config, _create_db=create_db, connect=connect)
        try:
            yield context
        finally:
            if drop_db:
                await context.connections.close_all(discard=False)
                for connection in context.connections.all():
                    await connection.db_delete()
                    context.connections.discard(connection.connection_name)


async def make_migrations(context: HareContext, apps_config: dict[str, dict[str, Any]]) -> list[Path]:
    assert context.apps is not None
    writers = await MigrationAutodetector(context.apps, apps_config).changes()
    written = [writer.write() for writer in writers]
    MigrationWriter.format_files(written)
    return written


async def migrate(context: HareContext, apps_config: dict[str, dict[str, Any]], *targets: tuple[str, str]) -> None:
    executor = MigrationExecutor(context.connections.get(CONNECTION), apps_config)
    await executor.migrate([MigrationTarget(app_label=app_label, name=name) for app_label, name in targets] or None)


async def get_table_names(context: HareContext) -> set[str]:
    return set(await SchemaIntrospector.get_table_names(context.connections.get(CONNECTION)))


async def get_foreign_key_target_tables(context: HareContext, table: str) -> dict[str, str]:
    [table_info] = await SchemaIntrospector.inspect_tables(context.connections.get(CONNECTION), [table])
    return {column: foreign_key.target_table for column, foreign_key in table_info.foreign_keys.items()}


def run_ruff(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed interpreter and arguments
        [sys.executable, "-m", "ruff", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def swappable_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SwappableProject:
    monkeypatch.syspath_prepend(str(tmp_path))
    return SwappableProject(tmp_path)


async def write_package_migrations(swappable_project: SwappableProject, tmp_path: Path) -> list[Path]:
    """The package author's own makemigrations run - no project, no setting."""
    package = swappable_project.write_package(with_migrations=False)
    apps_config = SwappableProject.get_apps_config(package)
    config = build_config(get_connection_config(tmp_path), apps_config)
    async with open_context(config, connect=False) as context:
        written = await make_migrations(context, apps_config)
    swappable_project.package_migration_files = written
    return written


# Configuration


def test_config_accepts_swappable_settings_and_keeps_them_in_to_dict() -> None:
    config = HareConfig.from_dict(
        {
            "connections": {"default": "sqlite://:memory:"},
            "apps": {"accounts": {"models": ["tests.testmodels"]}},
            "swappable": {"USER_MODEL": "accounts.Author"},
        }
    )
    assert config.swappable == {"USER_MODEL": "accounts.Author"}
    assert config.to_dict()["swappable"] == {"USER_MODEL": "accounts.Author"}
    assert HareConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    ("swappable_settings", "message"),
    [
        pytest.param(["USER_MODEL"], "must be a mapping", id="not_a_mapping"),
        pytest.param({"user_model": "accounts.Author"}, "upper-case identifier", id="lower_case_name"),
        pytest.param({"": "accounts.Author"}, "upper-case identifier", id="empty_name"),
        pytest.param({"USER-MODEL": "accounts.Author"}, "upper-case identifier", id="dash_in_name"),
        pytest.param({"USER_MODEL": "Author"}, '"app_label.ModelName"', id="label_without_app"),
        pytest.param({"USER_MODEL": "accounts.models.Author"}, '"app_label.ModelName"', id="label_with_module"),
        pytest.param({"USER_MODEL": 1}, '"app_label.ModelName"', id="label_not_a_string"),
        pytest.param({"USER_MODEL": "missing.Author"}, 'no app "missing" is configured', id="unknown_app"),
    ],
)
def test_config_rejects_malformed_swappable_settings(swappable_settings: Any, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        HareConfig.from_dict(
            {
                "connections": {"default": "sqlite://:memory:"},
                "apps": {"accounts": {"models": ["tests.testmodels"]}},
                "swappable": swappable_settings,
            }
        )


@pytest.mark.parametrize("extension", [".json", ".yml"])
def test_config_file_reads_swappable_settings(tmp_path: Path, extension: str) -> None:
    data = {
        "connections": {"default": "sqlite://:memory:"},
        "apps": {"accounts": {"models": ["tests.testmodels"]}},
        "swappable": {"USER_MODEL": "accounts.Author"},
    }
    config_file = tmp_path / f"config{extension}"
    config_file.write_text(json.dumps(data) if extension == ".json" else yaml.safe_dump(data), encoding="utf-8")
    assert HareConfig.from_config_file(str(config_file)).swappable == {"USER_MODEL": "accounts.Author"}


@pytest.mark.asyncio
async def test_init_rejects_a_setting_pointing_at_a_missing_or_abstract_model(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    apps_config = SwappableProject.get_apps_config(package)
    for label, message in (
        ("pkg.Nobody", 'app "pkg" has no such model'),
        ("pkg.BaseMember", "abstract model"),
    ):
        config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: label})
        with pytest.raises(ConfigurationError, match=message):
            async with open_context(config, connect=False):
                pass


@pytest.mark.asyncio
async def test_init_rejects_a_chain_of_swapped_models(swappable_project: SwappableProject, tmp_path: Path) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    # OTHER_MODEL -> pkg.Member, which MEMBER_MODEL itself swaps for proj.Account.
    config = build_config(
        get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account", "OTHER_MODEL": "pkg.Member"}
    )
    with pytest.raises(ConfigurationError, match="point OTHER_MODEL at the final model directly"):
        async with open_context(config, connect=False):
            pass


@pytest.mark.asyncio
async def test_swappable_label_and_model_follow_the_setting(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    async with open_context(build_config(get_connection_config(tmp_path), apps_config), connect=False):
        assert Hare.swappable_label(SETTING) == "pkg.Member"
        assert Hare.get_swappable_model(SETTING).__name__ == "Member"
        with pytest.raises(ConfigurationError, match='Unknown swappable setting "OTHER_MODEL"'):
            Hare.swappable_label("OTHER_MODEL")

    other_package = swappable_project.write_package(with_migrations=False)
    other_project = swappable_project.write_project(other_package)
    other_apps_config = SwappableProject.get_apps_config(other_package, other_project)
    config = build_config(get_connection_config(tmp_path), other_apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False):
        assert Hare.swappable_label(SETTING) == "proj.Account"
        assert Hare.get_swappable_model(SETTING).__name__ == "Account"


# Meta.swappable and relations


@pytest.mark.asyncio
async def test_default_model_stays_regular_without_the_setting_or_with_the_setting_on_itself(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    for swappable_settings in (None, {SETTING: "pkg.Member"}):
        package = swappable_project.write_package(with_migrations=False)
        apps_config = SwappableProject.get_apps_config(package)
        config = build_config(get_connection_config(tmp_path), apps_config, swappable_settings)
        async with open_context(config, create_db=True, drop_db=True) as context:
            member_model = context.get_model(PACKAGE_APP, "Member")
            consent_model = context.get_model(PACKAGE_APP, "Consent")
            assert member_model._meta.swapped is None
            assert consent_model._meta.fields_map["member"].related_model is member_model  # type: ignore[attr-defined]
            await context.generate_schemas()
            member = await member_model.objects.create(id=1, name="ann")
            await consent_model.objects.create(id=1, member=member)
            assert [consent.id for consent in await member.consents] == [1]


@pytest.mark.asyncio
async def test_swapped_default_model_has_no_table_and_refuses_queries(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, create_db=True, drop_db=True) as context:
        member_model = context.get_model(PACKAGE_APP, "Member")
        account_model = context.get_model(PROJECT_APP, "Account")
        consent_model = context.get_model(PACKAGE_APP, "Consent")
        assert member_model._meta.swapped == "proj.Account"
        assert account_model._meta.swapped is None
        for field_name in ("member", "owner", "witnesses"):
            assert consent_model._meta.fields_map[field_name].related_model is account_model  # type: ignore[attr-defined]
        assert "consents" in account_model._meta.fields_map
        assert "consents" not in member_model._meta.fields_map

        message = '"pkg.Member" has been swapped for "proj.Account" by the MEMBER_MODEL setting'
        with pytest.raises(ConfigurationError, match=message):
            member_model.objects.all()
        with pytest.raises(ConfigurationError, match=message):
            await member_model.objects.create(id=1, name="ann")

        await context.generate_schemas()
        table_names = await get_table_names(context)
        assert account_model._meta.db_table in table_names
        assert member_model._meta.db_table not in table_names
        account = await account_model.objects.create(id=1, name="ann")
        consent = await consent_model.objects.create(id=1, member=account, owner=account)
        await consent.witnesses.add(account)
        assert [consent.id for consent in await account.consents] == [1]
        assert [consent.id for consent in await account.witnessed_consents] == [1]


@pytest.mark.asyncio
async def test_swapped_default_model_is_left_out_of_drift(swappable_project: SwappableProject, tmp_path: Path) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, create_db=True, drop_db=True) as context:
        await context.generate_schemas()
        assert context.apps is not None
        result = await detect_drift_for_alias(context.apps, apps_config, CONNECTION)
        assert result.operations == []
        assert result.untracked_tables == []
        assert result.mismatched_columns == []


@pytest.mark.asyncio
async def test_relation_fields_keep_the_swappable_reference(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False) as context:
        consent_model = context.get_model(PACKAGE_APP, "Consent")
        for field_name in ("member", "owner", "witnesses"):
            field = consent_model._meta.fields_map[field_name]
            assert field.model_name == swappable(SETTING)  # type: ignore[attr-defined]
            _path, args, kwargs = field.deconstruct()
            assert kwargs.get("model_name", args[0] if args else None) == swappable(SETTING)


@pytest.mark.asyncio
async def test_a_relation_naming_a_swapped_model_directly_is_rejected(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(
        package,
        PROJECT_MODELS_SOURCE
        + textwrap.dedent(
            """
            class Note(BaseMember):
                author = fields.ForeignKeyField("pkg.Member", related_name="notes")
            """
        ),
    )
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    with pytest.raises(ConfigurationError, match=r'declare it with swappable\("MEMBER_MODEL"\) instead'):
        async with open_context(config, connect=False):
            pass


# Migrations


@pytest.mark.asyncio
async def test_makemigrations_writes_the_setting_and_a_swappable_dependency(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    [migration_file] = await write_package_migrations(swappable_project, tmp_path)
    source = migration_file.read_text(encoding="utf-8")
    assert "from hare.models import swappable" in source
    for field_class in ("ForeignKeyField", "OneToOneField", "ManyToManyField"):
        assert re.search(rf'fields\.{field_class}\(\s*swappable\("MEMBER_MODEL"\)', source), field_class
    assert 'dependencies = [migrations.swappable_dependency("MEMBER_MODEL")]' in source
    assert '"swappable": "MEMBER_MODEL"' in source
    lint = run_ruff("check", "--no-cache", "--select", "E,F,W,I", str(migration_file))
    assert lint.returncode == 0, lint.stdout + lint.stderr
    formatting = run_ruff("format", "--no-cache", "--check", str(migration_file))
    assert formatting.returncode == 0, formatting.stdout + formatting.stderr

    # Unchanged models write nothing more - with or without the setting.
    package = swappable_project.write_package()
    apps_config = SwappableProject.get_apps_config(package)
    async with open_context(build_config(get_connection_config(tmp_path), apps_config), connect=False) as ctx:
        assert await make_migrations(ctx, apps_config) == []


@pytest.mark.asyncio
async def test_changing_the_setting_changes_no_package_migration(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    await write_package_migrations(swappable_project, tmp_path)
    package = swappable_project.write_package()
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False) as context:
        written = await make_migrations(context, apps_config)
    assert [path.parent.parent.name for path in written] == [project]
    project_source = written[0].read_text(encoding="utf-8")
    assert 'name="Account"' in project_source
    assert "swappable" not in project_source


async def migrate_package_and_project(
    swappable_project: SwappableProject, tmp_path: Path, swappable_settings: dict[str, str] | None
) -> None:
    """Applies the package's migrations with a project, then rolls both back to zero."""
    package = swappable_project.write_package()
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    connection_config = get_connection_config(tmp_path)
    config = build_config(connection_config, apps_config, swappable_settings)
    async with open_context(config, connect=False) as context:
        await make_migrations(context, apps_config)
    async with open_context(config, create_db=True, drop_db=True) as context:
        await migrate(context, apps_config)
        member_model = context.get_model(PACKAGE_APP, "Member")
        account_model = context.get_model(PROJECT_APP, "Account")
        consent_model = context.get_model(PACKAGE_APP, "Consent")
        target_model = account_model if swappable_settings else member_model
        table_names = await get_table_names(context)
        assert target_model._meta.db_table in table_names
        assert account_model._meta.db_table in table_names
        assert (member_model._meta.db_table in table_names) is not bool(swappable_settings)
        foreign_key_targets = await get_foreign_key_target_tables(context, consent_model._meta.db_table)
        assert foreign_key_targets["member_id"] == target_model._meta.db_table
        assert foreign_key_targets["owner_id"] == target_model._meta.db_table
        through_table = consent_model._meta.fields_map["witnesses"].through  # type: ignore[attr-defined]
        # Named by the field, whichever model the setting points at.
        assert through_table == f"{consent_model._meta.db_table}_witnesses"
        assert through_table in table_names
        assert target_model._meta.db_table in (await get_foreign_key_target_tables(context, through_table)).values()

        target = await target_model.objects.create(id=1, name="ann")
        consent = await consent_model.objects.create(id=1, member=target, owner=target)
        await consent.witnesses.add(target)
        assert [consent.id for consent in await target.consents] == [1]
        assert context.apps is not None
        drift = await detect_drift_for_alias(context.apps, apps_config, CONNECTION)
        assert (drift.operations, drift.untracked_tables, drift.mismatched_columns) == ([], [], [])
        await consent_model.objects.all().delete()
        await target_model.objects.all().delete()

        await migrate(context, apps_config, (PACKAGE_APP, ZERO_MIGRATION))
        await migrate(context, apps_config, (PROJECT_APP, ZERO_MIGRATION))
        table_names = await get_table_names(context)
        for model in (member_model, account_model, consent_model):
            assert model._meta.db_table not in table_names
        assert through_table not in table_names


@pytest.mark.asyncio
async def test_package_migrations_point_at_the_project_model_the_setting_names(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    if not DatabaseUnderTest.get_dialect().supports_foreign_keys:
        pytest.skip("The foreign key targets are read from the database")
    await write_package_migrations(swappable_project, tmp_path)
    await migrate_package_and_project(swappable_project, tmp_path, {SETTING: "proj.Account"})


@pytest.mark.asyncio
async def test_package_migrations_point_at_the_default_model_without_the_setting(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    if not DatabaseUnderTest.get_dialect().supports_foreign_keys:
        pytest.skip("The foreign key targets are read from the database")
    await write_package_migrations(swappable_project, tmp_path)
    await migrate_package_and_project(swappable_project, tmp_path, None)


@pytest.mark.asyncio
async def test_package_migration_depends_on_the_first_migration_of_the_setting_app(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    await write_package_migrations(swappable_project, tmp_path)
    package = swappable_project.write_package()
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False) as context:
        await make_migrations(context, apps_config)
        loader = MigrationLoader(apps_config, NoopRecorder())
        await loader.build_graph()
        plan = [str(key) for key in loader.graph.forwards_plan(loader.graph.get_single_leaf(PACKAGE_APP))]
    assert plan.index("proj.0001_initial") < plan.index("pkg.0001_initial")


@pytest.mark.asyncio
async def test_a_package_project_cycle_is_split_into_two_migrations(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    if not DatabaseUnderTest.get_dialect().supports_foreign_keys:
        pytest.skip("The foreign key targets are read from the database")
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package, CYCLIC_PROJECT_MODELS_SOURCE)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False) as context:
        written = await make_migrations(context, apps_config)
    assert len(written) == 3
    async with open_context(config, create_db=True, drop_db=True) as context:
        await migrate(context, apps_config)
        consent_model = context.get_model(PACKAGE_APP, "Consent")
        account_model = context.get_model(PROJECT_APP, "Account")
        consent_targets = await get_foreign_key_target_tables(context, consent_model._meta.db_table)
        account_targets = await get_foreign_key_target_tables(context, account_model._meta.db_table)
        assert consent_targets["member_id"] == account_model._meta.db_table
        assert account_targets["favorite_consent_id"] == consent_model._meta.db_table
        await migrate(context, apps_config, (PACKAGE_APP, ZERO_MIGRATION))
        await migrate(context, apps_config, (PROJECT_APP, ZERO_MIGRATION))
        table_names = await get_table_names(context)
        assert consent_model._meta.db_table not in table_names
        assert account_model._meta.db_table not in table_names


@pytest.mark.asyncio
async def test_changing_the_setting_after_migrating_is_reported(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    if not DatabaseUnderTest.get_dialect().supports_foreign_keys:
        pytest.skip("The foreign key targets are read from the database")
    await write_package_migrations(swappable_project, tmp_path)
    connection_config = get_connection_config(tmp_path)

    package = swappable_project.write_package()
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(connection_config, apps_config)
    async with open_context(config, connect=False) as context:
        await make_migrations(context, apps_config)
    project_migration_files = sorted((tmp_path / project / "migrations").glob("0*.py"))
    async with open_context(config, create_db=True) as context:
        await migrate(context, apps_config)

    # The same database, now with the setting pointing at the project's model.
    other_package = swappable_project.write_package()
    other_project = swappable_project.write_project(other_package)
    for migration_file in project_migration_files:
        shutil.copy(migration_file, tmp_path / other_project / "migrations" / migration_file.name)
    other_apps_config = SwappableProject.get_apps_config(other_package, other_project)
    other_config = build_config(connection_config, other_apps_config, {SETTING: "proj.Account"})
    async with open_context(other_config, drop_db=True) as context:
        assert context.apps is not None
        drift = await detect_drift_for_alias(context.apps, other_apps_config, CONNECTION)
        [mismatch] = [mismatch for mismatch in drift.mismatched_columns if mismatch.column == "member_id"]
        assert "the MEMBER_MODEL setting points at" in mismatch.detail
        message = "A swappable model setting changed after its tables were created"
        with pytest.raises(ConfigurationError, match=message):
            await migrate(context, other_apps_config)


@pytest.mark.asyncio
async def test_swappable_dependency_needs_an_initialized_hare(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    await write_package_migrations(swappable_project, tmp_path)
    package = swappable_project.write_package()
    apps_config = SwappableProject.get_apps_config(package)
    context_token = HareContext.current_context.set(None)
    global_context = HareContext.global_context
    HareContext.global_context = None
    try:
        with pytest.raises(ConfigurationError, match=r"only known after Hare.init\(\)"):
            swappable_dependency(SETTING)
        with pytest.raises(MigrationLoadError, match=r"only known after Hare.init\(\)"):
            MigrationLoader(apps_config, NoopRecorder()).load_disk()
    finally:
        HareContext.global_context = global_context
        HareContext.current_context.reset(context_token)


@pytest.mark.asyncio
async def test_swappable_dependency_is_the_first_migration_of_the_setting_app(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False):
        dependency = swappable_dependency(SETTING)
        assert isinstance(dependency, SwappableDependency)
        assert dependency == ("proj", FIRST_MIGRATION)
        assert dependency.setting == SETTING


def test_naming_migrations_keeps_swappable_dependencies() -> None:
    from hare.migrations.constants import FIRST_MIGRATION as first_migration

    class FixedDependency(SwappableDependency):
        def __new__(cls, setting: str) -> FixedDependency:
            dependency = tuple.__new__(cls, ("proj", first_migration))
            dependency.setting = setting
            return dependency

    swappable_member_dependency = FixedDependency(SETTING)
    package_writer = MigrationWriter(
        "0002_auto", PACKAGE_APP, [], dependencies=[("pkg", "0001_initial"), swappable_member_dependency]
    )
    MigrationMaker.rename_writers([package_writer], "consents")
    assert package_writer.name == "0002_consents"
    assert package_writer.dependencies == [("pkg", "0001_initial"), swappable_member_dependency]
    assert isinstance(package_writer.dependencies[1], SwappableDependency)
    assert "migrations.swappable_dependency('MEMBER_MODEL')" in package_writer.as_string()


@pytest.mark.asyncio
async def test_migration_state_knows_the_swapped_model_has_no_table(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    await write_package_migrations(swappable_project, tmp_path)
    package = swappable_project.write_package()
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, connect=False) as context:
        await make_migrations(context, apps_config)
        assert context.apps is not None
        autodetector = MigrationAutodetector(context.apps, apps_config)
        await autodetector.loader.build_graph()
        state = await autodetector._project_state()
        assert state.models[(PACKAGE_APP, "Member")].options["swappable"] == SETTING
        assert state.is_swapped_model(PACKAGE_APP, "Member")
        assert not state.is_swapped_model(PACKAGE_APP, "Consent")
        assert not state.is_swapped_model(PROJECT_APP, "Account")
        assert sorted(model.__name__ for model in state.get_models_with_tables()) == ["Account", "Consent"]


@pytest.mark.asyncio
async def test_truncate_all_models_skips_the_swapped_model(
    swappable_project: SwappableProject, tmp_path: Path
) -> None:
    package = swappable_project.write_package(with_migrations=False)
    project = swappable_project.write_project(package)
    apps_config = SwappableProject.get_apps_config(package, project)
    config = build_config(get_connection_config(tmp_path), apps_config, {SETTING: "proj.Account"})
    async with open_context(config, create_db=True, drop_db=True) as context:
        await context.generate_schemas()
        account_model = context.get_model(PROJECT_APP, "Account")
        consent_model = context.get_model(PACKAGE_APP, "Consent")
        account = await account_model.objects.create(id=1, name="ann")
        consent = await consent_model.objects.create(id=1, member=account, owner=account)
        await consent.witnesses.add(account)

        await truncate_all_models()

        assert await account_model.objects.all().count() == 0
        assert await consent_model.objects.all().count() == 0
        through_table = consent_model._meta.fields_map["witnesses"].through  # type: ignore[attr-defined]
        _count, rows = await context.connections.get(CONNECTION).execute(f"SELECT * FROM {through_table}")
        assert rows == []
