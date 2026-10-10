from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib
import io
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from hare import Hare
from hare.cli import hare_cli as cli_module
from hare.cli.commands import migration_commands
from hare.cli.context import CommandContext
from hare.cli.exceptions import CLIError
from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, OperationalError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.loading.graph import MigrationKey
from hare.migrations.making import migration_maker
from hare.migrations.writer import MigrationWriter
from hare.transactions.distributed import DistributedCoordinator
from tests.utils.database_under_test import DatabaseUnderTest


@contextlib.contextmanager
def _override_hare_apps(value: Any):
    """Temporarily replace Hare.apps, bypassing the metaclass protection.

    Uses type.__setattr__ to bypass HareMeta.__setattr__ which
    prevents accidental shadowing of classproperty descriptors.
    """
    original = Hare.__dict__["apps"]
    type.__setattr__(Hare, "apps", value)
    try:
        yield
    finally:
        type.__setattr__(Hare, "apps", original)


def _write_package(tmp_path: Path, name: str) -> Path:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("", encoding="utf-8")
    return pkg


def _write_migrations(pkg: Path, migration_names: list[str]) -> None:
    migrations = pkg / "migrations"
    migrations.mkdir()
    (migrations / "__init__.py").write_text("", encoding="utf-8")
    for name in migration_names:
        (migrations / f"{name}.py").write_text(
            """
from hare.migrations.migration import Migration


class Migration(Migration):
    pass
""".lstrip(),
            encoding="utf-8",
        )


def _write_migration_with_dependencies(pkg: Path, name: str, dependencies: list[tuple[str, str]]) -> None:
    """Writes one migration with an explicit `dependencies` list - `_write_migrations` above
    always writes dependency-free migrations, which can't express a cross-app dependency cycle."""
    migrations = pkg / "migrations"
    if not migrations.exists():
        migrations.mkdir()
        (migrations / "__init__.py").write_text("", encoding="utf-8")
    (migrations / f"{name}.py").write_text(
        f"""
from hare.migrations.migration import Migration


class Migration(Migration):
    dependencies = {dependencies!r}
""".lstrip(),
        encoding="utf-8",
    )


def _purge_cli_app_modules() -> None:
    """cli_app is reused (same dotted name, different tmp_path) across many tests in this file -
    load_disk() only reload()s the migrations PACKAGE module, not each migration submodule, so a
    stale cached "0001_initial" from an earlier test would otherwise win over the just-written
    file. Call both before and after a test that writes a real "0001_initial" under cli_app."""
    for mod in list(sys.modules):
        if mod.startswith("cli_app"):
            del sys.modules[mod]


def _write_settings(tmp_path: Path, content: str, module_name: str) -> str:
    (tmp_path / f"{module_name}.py").write_text(content, encoding="utf-8")
    return module_name


async def _run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


@pytest.mark.asyncio
async def test_init_creates_migrations_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "init"])
    assert result.exit_code == 0

    migrations_path = tmp_path / "cli_app" / "migrations"
    assert migrations_path.exists()
    assert (migrations_path / "__init__.py").exists()
    assert "cli_app.migrations" in result.output


@pytest.mark.asyncio
async def test_init_top_level_migrations_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "init"])
    assert result.exit_code == 0

    migrations_path = tmp_path / "migrations"
    assert migrations_path.exists()
    assert (migrations_path / "__init__.py").exists()
    assert "migrations" in result.output


@pytest.mark.asyncio
async def test_migrate_passes_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    called: dict[str, Any] = {}

    async def fake_migrate(**kwargs) -> None:
        called.update(kwargs)

    monkeypatch.setattr(migration_commands, "migrate_api", fake_migrate)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "app", "0001_initial"])
    assert result.exit_code == 0
    assert called["target"] == "app.0001_initial"
    assert called["app_labels"] is None


@pytest.mark.asyncio
async def test_migrate_accepts_dotted_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    called: dict[str, object] = {}

    async def fake_migrate(**kwargs) -> None:
        called.update(kwargs)

    monkeypatch.setattr(migration_commands, "migrate_api", fake_migrate)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "app.0001_initial"])
    assert result.exit_code == 0
    assert called["target"] == "app.0001_initial"
    assert called["app_labels"] is None


@pytest.mark.asyncio
async def test_migrate_without_explicit_migrations_key_applies_on_disk_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_migrate used to read the raw "migrations" config key with no fallback - an app that
    relied on the same models->migrations inference makemigrations already uses to scaffold its
    package (no explicit "migrations" key at all) was silently treated as having no migrations,
    so migrate reported "No migrations to apply" and never created the table."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cli_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code == 0, result.output
        assert "APPLY" in result.output
        assert "app.0001_initial" in result.output

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "widget" in tables
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_migrate_composite_target_o2o_unique_constraint_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Apps._init_fk_o2o_field()`'s own auto-registration of a composite UniqueConstraint for a
    OneToOneField targeting a composite-PK model used to run twice for the same logical model: once
    on the live, directly-imported model class (loaded when the CLI reads the app's own models.py to
    build "current state" for makemigrations' own diff), and again on a SEPARATE class `migrate`
    renders from the just-written migration's own serialized state (`ModelState.render()`) - which
    already captured the live class's post-init `Meta.constraints` (including the auto-added
    composite constraint) into the migration's own `CreateModel` operation. A bare, unconditional
    append duplicated the constraint on the rendered class, and `CreateModel`'s own DDL (which
    applies every entry of `model._meta.constraints` once) then tried to create the identical UNIQUE
    index twice in the same `CREATE TABLE`/constraint-application pass, failing with "index ...
    already exists" - reproduced here through the real CLI, not by calling internals directly, since
    that's the exact call sequence (makemigrations, purge cached modules, migrate) that triggers it."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Versioned(Model):
    id = fields.IntField()
    version = fields.IntField()
    pk = CompositePrimaryKey("id", "version")


class Linked(Model):
    id = fields.IntField(primary_key=True)
    target = fields.OneToOneField("app.Versioned", null=True, related_name="linked")
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cli_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code == 0, result.output
        assert "already exists" not in result.output

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            unique_indexes = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='linked' "
                    "AND sql LIKE 'CREATE UNIQUE%'"
                )
            ]
        finally:
            conn.close()
        assert len(unique_indexes) == 1, f"expected exactly one unique index, found {unique_indexes}"
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_migrate_composite_pk_owner_m2m_through_table_columns_not_doubled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Apps.init_relations()`'s auto-derivation of an auto-managed ManyToManyField's
    through-table column names (`expand_many_to_many_key`) used to run twice for the same logical model,
    same root cause as the composite-target O2O UniqueConstraint bug above: once on the live,
    directly-imported model class (building `makemigrations`' own "current state"), and again on
    a SEPARATE class `migrate` renders from the migration's own serialized state. `Field.deconstruct()`'s
    generic kwargs-from-signature loop had no way to tell "auto-derived on the first pass" apart
    from "the user's own explicit override", so it serialized the ALREADY-EXPANDED first
    component of a composite-PK owner's `backward_key` straight into the migration file
    (`backward_key='book_isbn'`). Replaying that migration re-ran the SAME expansion on it as if
    it were a fresh override, expanding an already-expanded key a SECOND time
    ('book_isbn_isbn'/'book_isbn_edition' instead of 'book_isbn'/'book_edition') - the through
    table's real DDL then didn't match the column names live app startup (which always begins
    with an empty backward_key) actually resolves for this same relation, breaking every
    .add()/.remove()/prefetch on it with "column does not exist"."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Book(Model):
    isbn = fields.IntField()
    edition = fields.IntField()
    tags = fields.ManyToManyField("app.Tag", related_name="books")
    pk = CompositePrimaryKey("isbn", "edition")

    class Meta:
        table = "book"


class Tag(Model):
    id = fields.IntField(primary_key=True)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cli_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code == 0, result.output

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            through_tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'book%tag%'")
            ]
            assert len(through_tables) == 1, f"expected exactly one through table, found {through_tables}"
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({through_tables[0]})")}
        finally:
            conn.close()
        assert "book_isbn" in columns, f"expected 'book_isbn' column, got {columns}"
        assert "book_edition" in columns, f"expected 'book_edition' column, got {columns}"
        assert "book_isbn_isbn" not in columns, f"through-table key was double-expanded: {columns}"
        assert "book_isbn_edition" not in columns, f"through-table key was double-expanded: {columns}"
    finally:
        _purge_cli_app_modules()


def _purge_cross_fk_app_modules() -> None:
    for mod in list(sys.modules):
        if mod.startswith(("cli_crossfk_a", "cli_crossfk_b")):
            del sys.modules[mod]


@pytest.mark.asyncio
async def test_makemigrations_first_time_cross_app_fk_gets_dependency_on_sibling_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MigrationAutodetector.changes() used to compute each target app's operations/dependencies
    strictly against what's already on disk - but nothing gets WRITTEN to disk until every target
    app's migration has already been computed (write() runs afterward, one writer at a time). So
    when two apps are migrated for the FIRST TIME TOGETHER in one `hare makemigrations` (the
    ordinary case for a brand-new multi-app project) and one has a ForeignKeyField into the other,
    the related app's own migration didn't exist on disk yet at dependency-computation time - the
    generated migration got dependencies=[] despite the real FK, so `migrate` was free to apply
    the two apps' initial migrations in either order, and the wrong order crashed with "still have
    uninitialized relations". Reproduced through the real CLI end to end (makemigrations with no
    target = every app at once, then migrate), not by calling MigrationAutodetector internals
    directly."""
    pkg_a = _write_package(tmp_path, "cli_crossfk_a")
    pkg_b = _write_package(tmp_path, "cli_crossfk_b")
    (pkg_a / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    category = fields.ForeignKeyField("crossfk_b.Category", related_name="widgets")
""".lstrip(),
        encoding="utf-8",
    )
    (pkg_b / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "crossfk_a": {{"models": ["cli_crossfk_a.models"], "default_connection": "default"}},
        "crossfk_b": {{"models": ["cli_crossfk_b.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cross_fk_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cross_fk_app_modules()

        migration_a_source = (pkg_a / "migrations" / "0001_initial.py").read_text(encoding="utf-8")
        assert 'dependencies = [("crossfk_b", "0001_initial")]' in migration_a_source, migration_a_source

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output
        assert "uninitialized relations" not in result.output
    finally:
        _purge_cross_fk_app_modules()


def _purge_cyclic_app_modules() -> None:
    for mod in list(sys.modules):
        if mod.startswith(("cli_cycle_a", "cli_cycle_b")):
            del sys.modules[mod]


def _write_cyclic_apps_config(tmp_path: Path) -> str:
    """Two apps whose single migration each depends on the other - a genuine 2-node cycle,
    used by both circular-dependency tests below."""
    pkg_a = _write_package(tmp_path, "cli_cycle_a")
    pkg_b = _write_package(tmp_path, "cli_cycle_b")
    _write_migration_with_dependencies(pkg_a, "0001_a", [("cycle_b", "0001_b")])
    _write_migration_with_dependencies(pkg_b, "0001_b", [("cycle_a", "0001_a")])
    return _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "cycle_a": {
            "models": ["cli_cycle_a.models"],
            "default_connection": "default",
            "migrations": "cli_cycle_a.migrations",
        },
        "cycle_b": {
            "models": ["cli_cycle_b.models"],
            "default_connection": "default",
            "migrations": "cli_cycle_b.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )


@pytest.mark.asyncio
async def test_migrate_default_target_raises_on_circular_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2-node cycle across apps leaves zero leaf nodes for either app, so the default
    (no explicit app/migration) `hare migrate` invocation - which plans from `leaf_nodes()` -
    used to find nothing to walk from and silently report success with nothing applied,
    instead of the CircularDependencyError this graph genuinely contains."""
    module_name = _write_cyclic_apps_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cyclic_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code != 0
        assert "circular" in result.output.lower()
        assert "No migrations to apply" not in result.output
    finally:
        _purge_cyclic_app_modules()


@pytest.mark.asyncio
async def test_migrate_explicit_target_raises_clean_error_on_circular_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit target does reach the cycle - confirms CircularDependencyError still fires
    for this path, and that it reaches the user as a clean CLIError, not a raw traceback."""
    module_name = _write_cyclic_apps_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cyclic_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "cycle_a", "0001_a"])
        assert result.exit_code != 0
        assert "circular" in result.output.lower()
        assert "Traceback" not in result.output
    finally:
        _purge_cyclic_app_modules()


def _purge_dangling_dep_app_modules() -> None:
    for mod in list(sys.modules):
        if mod.startswith(("cli_dangling_a", "cli_dangling_b")):
            del sys.modules[mod]


def _write_dangling_dependency_apps_config(tmp_path: Path) -> str:
    """app_b's own migration depends on an app_a migration that was never created - the shape
    left behind by the documented "safe" squashmigrations workflow (squash app_a, then remove
    its old migration files) when some OTHER app still names one of those removed migrations in
    its own `dependencies` - used by all three tests below."""
    pkg_a = _write_package(tmp_path, "cli_dangling_a")
    pkg_b = _write_package(tmp_path, "cli_dangling_b")
    _write_migration_with_dependencies(pkg_a, "0001_a", [])
    _write_migration_with_dependencies(pkg_b, "0001_b", [("dangling_a", "0002_never_existed")])
    return _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "dangling_a": {
            "models": ["cli_dangling_a.models"],
            "default_connection": "default",
            "migrations": "cli_dangling_a.migrations",
        },
        "dangling_b": {
            "models": ["cli_dangling_b.models"],
            "default_connection": "default",
            "migrations": "cli_dangling_b.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_args", [["heads"], ["makemigrations"], ["squashmigrations", "dangling_b", "0001_initial"]]
)
async def test_dangling_cross_app_dependency_maps_to_clean_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_args: list[str]
) -> None:
    """MigrationGraph.validate_consistency() raises a bare ValueError (DummyNode.raise_error())
    for a dependency naming a migration that doesn't exist - migrate()/sqlmigrate() already
    translated it into a clean CLIError, but heads()/makemigrations()/squashmigrations() only
    caught ConfigurationError/CircularDependencyError, letting this ValueError escape as a raw,
    multi-frame traceback instead."""
    module_name = _write_dangling_dependency_apps_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_dangling_dep_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", *command_args])
        assert result.exit_code != 0
        assert "nonexistent parent" in result.output
        assert "Traceback (most recent call last)" not in result.output
    finally:
        _purge_dangling_dep_app_modules()


def _purge_forked_app_modules() -> None:
    for mod in list(sys.modules):
        if mod.startswith("cli_forked"):
            del sys.modules[mod]


def _write_forked_app_config(tmp_path: Path) -> str:
    """One app whose migration graph forks in two: 0002_a and 0002_b both depend on the same
    0001_initial parent, with no merge migration between them - a genuine conflicting-heads
    scenario, used by both tests below."""
    pkg = _write_package(tmp_path, "cli_forked")
    _write_migration_with_dependencies(pkg, "0001_initial", [])
    _write_migration_with_dependencies(pkg, "0002_a", [("forked", "0001_initial")])
    _write_migration_with_dependencies(pkg, "0002_b", [("forked", "0001_initial")])
    return _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "forked": {
            "models": ["cli_forked.models"],
            "default_connection": "default",
            "migrations": "cli_forked.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )


@pytest.mark.asyncio
async def test_migrate_default_target_raises_on_conflicting_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two migrations forking off the same parent with no merge migration between them used to
    be silently resolved by picking (or applying) every leaf independently - `hare migrate` with
    no explicit target must instead raise a clear error naming the conflict, the same way
    Django's own migration executor does, rather than silently applying both branches."""
    module_name = _write_forked_app_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_forked_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code != 0
        assert "conflicting migrations" in result.output.lower()
        assert "0002_a" in result.output
        assert "0002_b" in result.output
        assert "Traceback" not in result.output
    finally:
        _purge_forked_app_modules()


@pytest.mark.asyncio
async def test_migrate_explicit_app_target_raises_on_conflicting_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit app-only target (resolving to that app's "__latest__") hits the exact same
    ambiguity as the bare default target above and must raise the same clear error."""
    module_name = _write_forked_app_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_forked_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "forked"])
        assert result.exit_code != 0
        assert "conflicting migrations" in result.output.lower()
        assert "Traceback" not in result.output
    finally:
        _purge_forked_app_modules()


@pytest.mark.asyncio
async def test_migrate_unexpected_exception_during_apply_maps_to_clean_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_migrate` only translated DBConnectionError/OperationalError (via
    `_database_error_boundary`) and CircularDependencyError/PartiallyAppliedMigrationError/
    ConfigurationError into a clean CLIError - any OTHER exception raised while actually
    applying a migration step used to propagate as a raw traceback instead. Here,
    `Migration.apply()`'s own `State.validate_relations_initialized()` safety net raises a bare
    RuntimeError because the migration's FK targets a model in an app that was never configured
    at all, so `StateApps.init_relations()`'s deferred-resolution logic can never resolve it."""
    pkg = _write_package(tmp_path, "cli_orphan_fk")
    migrations_dir = pkg / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "__init__.py").write_text("", encoding="utf-8")
    (migrations_dir / "0001_initial.py").write_text(
        """
from hare import fields
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel


class Migration(Migration):
    operations = [
        CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("category", fields.ForeignKeyField("ghost_app.GhostModel", related_name="widgets")),
            ],
        ),
    ]
""".lstrip(),
        encoding="utf-8",
    )

    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "orphan_fk": {
            "models": ["cli_orphan_fk.models"],
            "default_connection": "default",
            "migrations": "cli_orphan_fk.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}_orphan_fk",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    for mod in list(sys.modules):
        if mod.startswith("cli_orphan_fk"):
            del sys.modules[mod]

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "uninitialized relations" in result.output.lower()
    finally:
        for mod in list(sys.modules):
            if mod.startswith("cli_orphan_fk"):
                del sys.modules[mod]


@pytest.mark.asyncio
async def test_migrate_zero_targets_the_state_before_the_first_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    called: dict[str, object] = {}

    async def fake_migrate(**kwargs) -> None:
        called.update(kwargs)

    monkeypatch.setattr(migration_commands, "migrate_api", fake_migrate)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "app", "zero"])
    assert result.exit_code == 0
    assert called["target"] == "app.zero"
    assert called["app_labels"] is None


@pytest.mark.asyncio
async def test_migrate_zero_keeps_full_config_for_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_package(tmp_path, "cli_accounts")
    _write_package(tmp_path, "cli_orders")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "accounts": {"models": ["cli_accounts.models"], "default_connection": "default"},
        "orders": {"models": ["cli_orders.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    called: dict[str, object] = {}

    async def fake_migrate(**kwargs) -> None:
        called.update(kwargs)

    monkeypatch.setattr(migration_commands, "migrate_api", fake_migrate)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate", "orders", "zero"])
    assert result.exit_code == 0
    assert called["target"] == "orders.zero"
    assert called["app_labels"] is None
    called_config = called["config"]
    if isinstance(called_config, dict):
        apps = called_config["apps"]
    else:
        assert isinstance(called_config, HareConfig)
        apps = called_config.apps
    assert set(apps.keys()) == {"accounts", "orders"}


@pytest.mark.asyncio
async def test_history_grouped_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    _write_package(tmp_path, "cli_other")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
        "other": {"models": ["cli_other.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    async def fake_init(**_kwargs) -> None:
        return None

    async def fake_applied(self) -> list[MigrationKey]:
        return [
            MigrationKey(app_label="app", name="0001_initial"),
            MigrationKey(app_label="other", name="0001_initial"),
        ]

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_commands.MigrationRecorder, "applied_migrations", fake_applied)
    monkeypatch.setattr(Connections, "get", lambda _name: object())

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "history"])
    assert result.exit_code == 0
    assert "Connection: default" in result.output
    assert "app:" in result.output
    assert "other:" in result.output
    assert "app 0001_initial" in result.output
    assert "other 0001_initial" in result.output


@pytest.mark.asyncio
async def test_heads_grouped_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_pkg = _write_package(tmp_path, "cli_app")
    other_pkg = _write_package(tmp_path, "cli_other")
    _write_migrations(app_pkg, ["0001_initial"])
    _write_migrations(other_pkg, ["0001_initial", "0002_more"])

    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
        "other": {
            "models": ["cli_other.models"],
            "default_connection": "default",
            "migrations": "cli_other.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_other", None)
    sys.modules.pop("cli_other.migrations", None)
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)
    importlib.import_module("cli_app.migrations")
    importlib.import_module("cli_other.migrations")

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "heads"])
    assert result.exit_code == 0
    assert "Connection: default" in result.output
    assert "app:" in result.output
    assert "other:" in result.output
    assert "app.0001_initial" in result.output
    assert "other.0001_initial" in result.output
    assert "other.0002_more" in result.output


@pytest.mark.asyncio
async def test_heads_on_circular_dependency_maps_to_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """heads() called loader.build_graph() with no try/except at all - a genuine circular
    dependency raised a raw ConfigurationError straight through the CLI instead of a clean
    CLIError, unlike migrate/makemigrations for the exact same graph shape."""
    module_name = _write_cyclic_apps_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cyclic_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "heads"])
        assert result.exit_code != 0
        assert "circular" in result.output.lower()
        assert "Traceback" not in result.output
    finally:
        _purge_cyclic_app_modules()


@pytest.mark.asyncio
async def test_heads_without_explicit_migrations_key_finds_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """heads() used to pass the raw "migrations" config key straight to MigrationLoader with no
    inference fallback - an app relying on models->migrations inference (no explicit
    "migrations" key) was silently treated as unmigrated, so heads reported "(no heads)" even
    though the migration genuinely existed on disk."""
    app_pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(app_pkg, ["0001_initial"])

    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)
    importlib.import_module("cli_app.migrations")

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "heads"])
    assert result.exit_code == 0, result.output
    assert "app.0001_initial" in result.output
    assert "no heads" not in result.output.lower()


@pytest.mark.asyncio
async def test_heads_with_app_label_and_cross_app_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """heads() used to filter apps to the requested app_labels BEFORE building the migration
    graph, unlike makemigrations/migrate (which all deliberately load every
    configured app first, precisely because a migration in the targeted app can depend on a
    concrete migration in another, unselected app). Requesting `heads` for just the app whose
    migration has a cross-app dependency crashed with an unhandled ValueError instead of
    working the same way it does with no app_labels filter at all."""
    accounts_pkg = _write_package(tmp_path, "cli_accounts2")
    orders_pkg = _write_package(tmp_path, "cli_orders2")
    _write_migrations(accounts_pkg, ["0001_initial"])
    migrations_dir = orders_pkg / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "__init__.py").write_text("", encoding="utf-8")
    (migrations_dir / "0001_initial.py").write_text(
        """
from hare.migrations.migration import Migration


class Migration(Migration):
    dependencies = [("accounts2", "0001_initial")]
""".lstrip(),
        encoding="utf-8",
    )

    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "accounts2": {
            "models": ["cli_accounts2.models"],
            "default_connection": "default",
            "migrations": "cli_accounts2.migrations",
        },
        "orders2": {
            "models": ["cli_orders2.models"],
            "default_connection": "default",
            "migrations": "cli_orders2.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}_heads_xapp",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    for mod in ("cli_accounts2", "cli_accounts2.migrations", "cli_orders2", "cli_orders2.migrations"):
        sys.modules.pop(mod, None)
    importlib.import_module("cli_accounts2.migrations")
    importlib.import_module("cli_orders2.migrations")

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "heads", "orders2"])
    assert result.exit_code == 0
    assert "orders2:" in result.output
    assert "orders2.0001_initial" in result.output
    assert "accounts2:" not in result.output


@pytest.mark.asyncio
async def test_drift_with_app_label_and_cross_app_relation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`drift()` used to load ONLY the filtered `app_labels` subset via `_select_apps()`, unlike
    every sibling command that accepts an app-label filter (makemigrations/migrate/
    heads/squashmigrations, all of which deliberately load the FULL app config first
    because a targeted app's model can hold a relation into another, unselected app). Requesting
    `drift` for just the app holding the cross-app FK crashed with "No app with name 'drift_appb'
    registered" instead of working the same way the unfiltered `hare drift` does, just scoped to
    reporting drift_appa's own drift."""
    appa_pkg = _write_package(tmp_path, "cli_drift_appa")
    appb_pkg = _write_package(tmp_path, "cli_drift_appb")
    # Category is Meta.managed = False - never given a CreateModel of its own and excluded from
    # the state drift() diffs models against (see MigrationAutodetector.current_state()), so its
    # table never enters drift's own "known tables for this connection" bookkeeping either way.
    # This isolates the test to the actual bug (app-loading crashing on the cross-app reference)
    # instead of also exercising detect_drift()'s separate, pre-existing "table owned by another,
    # unfiltered app on the same connection reports as untracked" scoping behavior.
    (appb_pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    class Meta:
        managed = False
""".lstrip(),
        encoding="utf-8",
    )
    (appa_pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    category = fields.ForeignKeyField("drift_appb.Category", related_name="widgets")
""".lstrip(),
        encoding="utf-8",
    )
    _write_migrations(appb_pkg, [])
    _write_migrations(appa_pkg, ["0001_initial"])
    (appa_pkg / "migrations" / "0001_initial.py").write_text(
        """
from hare import fields
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel


class Migration(Migration):
    operations = [
        CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("category", fields.ForeignKeyField("drift_appb.Category", related_name="widgets", db_index=True)),
            ],
        ),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift_test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "drift_appa": {{
            "models": ["cli_drift_appa.models"],
            "default_connection": "default",
            "migrations": "cli_drift_appa.migrations",
        }},
        "drift_appb": {{
            "models": ["cli_drift_appb.models"],
            "default_connection": "default",
            "migrations": "cli_drift_appb.migrations",
        }},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}_drift",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    def _purge_drift_app_modules() -> None:
        for mod in list(sys.modules):
            if mod.startswith(("cli_drift_appa", "cli_drift_appb")):
                del sys.modules[mod]

    _purge_drift_app_modules()
    importlib.import_module("cli_drift_appa.migrations")
    importlib.import_module("cli_drift_appb.migrations")

    try:
        migrate_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert migrate_result.exit_code == 0, migrate_result.output

        # Widget's class object (and its `_meta._foreign_key_or_one_to_one_inited` flag) would otherwise survive in
        # `sys.modules` from the `migrate` run above, which already loads every app - reusing that
        # cached, already-resolved class would skip `Apps._init_fk_o2o_field()` entirely on the
        # next command and silently hide the bug this test exists to catch. A real `hare drift`
        # invocation is a separate OS process with no such carryover, so each command below gets
        # a fresh import to match.
        _purge_drift_app_modules()
        unfiltered = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert unfiltered.exit_code == 0, unfiltered.output

        _purge_drift_app_modules()
        filtered = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift", "drift_appa"])
        assert filtered.exit_code == 0, filtered.output
        assert "No app with name" not in filtered.output
    finally:
        _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_does_not_report_a_managed_false_table_as_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Meta.managed = False model's table is real and EXPECTED to exist (the whole point of
    managed = False is a runtime/live-introspected model wrapping someone else's already-existing
    table, e.g. Hare.register_live_model()) - current_state() deliberately excludes it from the
    state drift() diffs against, which used to also mean its table never entered known_tables at
    all, so `hare drift` reported "Untracked table" for it on every single run, permanently, with
    no way to silence it short of removing the model."""
    pkg = _write_package(tmp_path, "cli_drift_unmanaged")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class LegacyOrders(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    class Meta:
        managed = False
        table = "legacy_orders"
""".lstrip(),
        encoding="utf-8",
    )
    _write_migrations(pkg, [])

    db_path = tmp_path / "drift_unmanaged_test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "cli_drift_unmanaged": {{
            "models": ["cli_drift_unmanaged.models"],
            "default_connection": "default",
            "migrations": "cli_drift_unmanaged.migrations",
        }},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}_drift_unmanaged",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    def _purge() -> None:
        for mod in list(sys.modules):
            if mod.startswith("cli_drift_unmanaged"):
                del sys.modules[mod]

    _purge()
    importlib.import_module("cli_drift_unmanaged.migrations")

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute('CREATE TABLE "legacy_orders" ("id" INTEGER PRIMARY KEY, "name" TEXT NOT NULL)')
        conn.commit()
    finally:
        conn.close()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert result.exit_code == 0, result.output
        assert "Untracked table" not in result.output
        assert "legacy_orders" not in result.output
    finally:
        _purge()


@pytest.mark.asyncio
async def test_makemigrations_writes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FakeAutodetector:
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, _apps, apps_config, **_kwargs) -> None:
            self.apps_config = apps_config

        async def changes(self) -> list[MigrationWriter]:
            return [
                MigrationWriter(
                    "0001_initial",
                    "app",
                    [],
                    migrations_module=self.apps_config["app"]["migrations"],
                )
            ]

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FakeAutodetector)

    with _override_hare_apps(object()):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--name", "add blog"])
        assert result.exit_code == 0

        migrations_path = tmp_path / "cli_app" / "migrations"
        assert (migrations_path / "0001_add_blog.py").exists()


@pytest.mark.asyncio
async def test_makemigrations_prints_ambiguous_rename_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MigrationAutodetector.warnings (populated by StateFieldDiff - see its own docstring for
    the ambiguous-rename shape this covers) must actually reach the terminal - makemigrations
    reads it right after calling changes() and prints each one, the same way it already prints
    a WARNING for squashmigrations dropping RunPython/RunSQL."""
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FakeAutodetector:
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, _apps, apps_config, **_kwargs) -> None:
            self.apps_config = apps_config
            self.warnings: list[str] = []

        async def changes(self) -> list[MigrationWriter]:
            self.warnings = ["Model Widget: added field 'headline' has the exact same type..."]
            return [
                MigrationWriter(
                    "0001_initial",
                    "app",
                    [],
                    migrations_module=self.apps_config["app"]["migrations"],
                )
            ]

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FakeAutodetector)

    with _override_hare_apps(object()):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--name", "add blog"])
        assert result.exit_code == 0
        assert "WARNING" in result.output
        assert "headline" in result.output


@pytest.mark.asyncio
async def test_makemigrations_prints_data_loss_warnings_under_their_own_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MigrationAutodetector.data_loss_warnings (populated by StateFieldDiff for a generated
    field turning into a plain field - see its own docstring) is a SEPARATE category from
    .warnings' ambiguous-rename advisories above: it has nothing to do with a rename, so it must
    be printed under its own "data loss risk" header, never under the "possible unrecognized
    rename(s)" one - that header used to be printed unconditionally for every entry in
    .warnings, including this unrelated type, misleadingly labeling a data-loss warning as a
    rename question."""
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FakeAutodetector:
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, _apps, apps_config, **_kwargs) -> None:
            self.apps_config = apps_config
            self.warnings: list[str] = []
            self.data_loss_warnings: list[str] = []

        async def changes(self) -> list[MigrationWriter]:
            self.data_loss_warnings = [
                "Model Widget: turning generated field 'total' into a plain field will DROP its "
                "already-computed values..."
            ]
            return [
                MigrationWriter(
                    "0001_initial",
                    "app",
                    [],
                    migrations_module=self.apps_config["app"]["migrations"],
                )
            ]

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FakeAutodetector)

    with _override_hare_apps(object()):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--name", "add blog"])
        assert result.exit_code == 0
        assert "data loss risk" in result.output
        assert "total" in result.output
        assert "possible unrecognized rename" not in result.output


@pytest.mark.asyncio
async def test_makemigrations_unknown_app_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """makemigrations deliberately loads EVERY configured app regardless of the caller's own
    APP_LABEL arguments (a migration in one app can depend on another app's models/migrations),
    so it never went through _select_apps(config, app_labels)'s own unknown-label validation the
    way every sibling command (squashmigrations/heads/history/drift/...) does - an unknown label
    used to reach apps_dict[label] (the --empty branch) or MigrationAutodetector's own
    self.apps_config[app_label] as a raw, uncaught KeyError instead of the same clean
    CLIUsageError every sibling command already raises for this mistake."""
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "ghost"])
    assert result.exit_code != 0
    assert "Unknown app label" in result.output


@pytest.mark.asyncio
async def test_makemigrations_no_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FakeAutodetector:
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, _apps, _apps_config, **_kwargs) -> None:
            return None

        async def changes(self) -> list[MigrationWriter]:
            return []

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FakeAutodetector)

    with _override_hare_apps(object()):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations"])
        assert result.exit_code == 0
        assert "No changes detected" in result.output


@pytest.mark.asyncio
async def test_makemigrations_check_reports_pending_changes_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--check"])
        assert result.exit_code == 1, result.output
        assert "app.0001_initial" in result.output
        assert "Create model Widget" in result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        assert [p.name for p in migrations_path.glob("*.py")] == ["__init__.py"]
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_makemigrations_dry_run_prints_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "app.0001_initial" in result.output
        assert "Create model Widget" in result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        assert [p.name for p in migrations_path.glob("*.py")] == ["__init__.py"]
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_makemigrations_check_exits_zero_when_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FakeAutodetector:
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, _apps, _apps_config, **_kwargs) -> None:
            return None

        async def changes(self) -> list[MigrationWriter]:
            return []

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FakeAutodetector)

    with _override_hare_apps(object()):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--check"])
        assert result.exit_code == 0
        assert "No changes detected" in result.output


@pytest.mark.asyncio
async def test_makemigrations_empty_cannot_combine_with_check_or_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--empty", "--check"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--empty", "--dry-run"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def _write_forked_migrations(pkg: Path) -> None:
    """0001_initial with two children (0002_a/0002_b) both depending on it directly - a genuine
    fork with no merge migration yet, the same shape test_graph.py's own
    test_get_single_leaf_raises_on_conflicting_heads uses to exercise MigrationGraph directly."""
    _write_migrations(pkg, ["0001_initial"])
    _write_migration_with_dependencies(pkg, "0002_a", [("app", "0001_initial")])
    _write_migration_with_dependencies(pkg, "0002_b", [("app", "0001_initial")])


@pytest.mark.asyncio
async def test_makemigrations_merge_creates_migration_depending_on_both_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_forked_migrations(pkg)
    db_path = tmp_path / "merge.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--merge"])
        assert result.exit_code == 0, result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        merge_files = list(migrations_path.glob("0003_merge_*.py"))
        assert len(merge_files) == 1, [p.name for p in migrations_path.glob("*.py")]
        content = merge_files[0].read_text(encoding="utf-8")
        assert '("app", "0002_a")' in content
        assert '("app", "0002_b")' in content
        assert "operations = [" in content and "ops." not in content.split("operations = [")[1]

        _purge_cli_app_modules()
        migrate_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert migrate_result.exit_code == 0, migrate_result.output
        assert "0003_merge" in migrate_result.output
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_makemigrations_merge_rejects_a_field_renamed_differently_on_each_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_build_merge_writers` used to write an empty merge migration for ANY fork, with no
    check that the two branches could actually be combined - the same field ('foo') renamed to
    'bar' on one branch and 'baz' on the other used to sail through as a "successful" merge,
    only failing much later when a real `migrate` run applied both branches together: one
    branch's migrations get committed, the other's `state_forward()` no longer matches
    (`IncompatibleStateError`), leaving the migration history permanently half-applied. Now
    caught up front, before the merge migration is ever written."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        "from hare import fields\nfrom hare.models import Model\n\n\n"
        "class Widget(Model):\n    id = fields.IntField(primary_key=True)\n    bar = fields.IntField()\n",
        encoding="utf-8",
    )
    migrations = pkg / "migrations"
    migrations.mkdir()
    (migrations / "__init__.py").write_text("", encoding="utf-8")
    (migrations / "0001_initial.py").write_text(
        """
from hare import migrations
from hare.migrations import operations as ops
from hare import fields


class Migration(migrations.Migration):
    initial = True
    operations = [
        ops.CreateModel(
            name='Widget',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('foo', fields.IntField()),
            ],
            options={'table': 'widget', 'app': 'app', 'primary_key_attribute': 'id'},
            bases=['Model'],
        ),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    (migrations / "0002_a.py").write_text(
        """
from hare import migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = [
        ops.RenameField(model_name='Widget', old_name='foo', new_name='bar'),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    (migrations / "0002_b.py").write_text(
        """
from hare import migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = [
        ops.RenameField(model_name='Widget', old_name='foo', new_name='baz'),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--merge"])
        assert result.exit_code != 0
        assert "Traceback (most recent call last)" not in result.output
        assert "can't merge migration history" in result.output.lower()
        assert not list((pkg / "migrations").glob("*merge*"))
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_makemigrations_merge_raises_when_single_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(pkg, ["0001_initial"])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app", "--merge"])
        assert result.exit_code == 1
        assert "Nothing to merge" in result.output
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_makemigrations_merge_requires_app_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--merge"])
    assert result.exit_code == 2
    assert "--merge requires" in result.output


@pytest.mark.asyncio
async def test_makemigrations_composite_pk_model_is_stable_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CreateModel.state_forward() derives its in-memory ModelState from CreateModel.model, the
    same reconstructed class database_forward() uses for DDL - when that reconstruction dropped
    the composite PK (falling back to a surrogate "id"), a second makemigrations run diffed the
    written migration's state ("id") against the real, unchanged model's state (composite),
    raising a spurious "changing primary key" ConfigurationError instead of finding no changes."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class ThingRevision(Model):
    thing_id = fields.IntField()
    revision = fields.IntField()
    name = fields.CharField(max_length=50)

    pk = fields.CompositePrimaryKey("thing_id", "revision")
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        first = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert first.exit_code == 0, first.output
        _purge_cli_app_modules()

        second = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert second.exit_code == 0, second.output
        assert "No changes detected" in second.output
        assert "primary key" not in second.output.lower()
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_squashmigrations_requires_at_least_two_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(pkg, ["0001_initial"])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0001_initial"])
    assert result.exit_code == 0
    assert "Only one migration" in result.output

    migrations_path = tmp_path / "cli_app" / "migrations"
    assert sorted(p.stem for p in migrations_path.glob("*.py") if p.stem != "__init__") == ["0001_initial"]


@pytest.mark.parametrize(
    ("error_match", "command", "argument"),
    [
        pytest.param(
            "Unknown app label", "squashmigrations", "ghost 0001_initial", id="squashmigrations_unknown_app_label"
        ),
        pytest.param("migration_name is required", "sqlmigrate", "app", id="sqlmigrate_requires_migration_name"),
        pytest.param(
            "--empty requires at least one APP_LABEL",
            "makemigrations",
            "--empty",
            id="makemigrations_empty_requires_app_label",
        ),
    ],
)
@pytest.mark.asyncio
async def test_migration_command_with_wrong_arguments_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_match, command, argument
) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", command, *argument.split()])
    assert result.exit_code != 0
    assert error_match in result.output


@pytest.mark.asyncio
async def test_squashmigrations_writes_single_file_with_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    _write_migration_with_dependencies(pkg, "0001_initial", [])
    _write_migration_with_dependencies(pkg, "0002_second", [("app", "0001_initial")])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)
    sys.modules.pop("cli_app.models", None)

    result = await _run_cli(
        ["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0002_second", "--squashed-name", "squashed"]
    )
    assert result.exit_code == 0, result.output
    assert "replaces 2 migration(s)" in result.output
    assert "0001_initial" in result.output
    assert "0002_second" in result.output

    migrations_path = tmp_path / "cli_app" / "migrations"
    # Numbered after the first migration it replaces - it stands in their place.
    migration_file = migrations_path / "0001_squashed.py"
    assert migration_file.exists()
    content = migration_file.read_text(encoding="utf-8")
    assert 'replaces = [("app", "0001_initial"), ("app", "0002_second")]' in content
    assert "initial = True" in content

    # old files are left untouched - deleting them is a manual step per the printed warning
    assert (migrations_path / "0001_initial.py").exists()
    assert (migrations_path / "0002_second.py").exists()


def _write_widget_chain(pkg: Path) -> None:
    """A real 3-migration chain: 0001 actually creates the `widget` table, 0002/0003 are no-ops
    chained onto it - close enough to a real history to exercise squashmigrations' numbering
    against genuine on-disk migration files instead of the blank ones _write_migrations writes."""
    migrations = pkg / "migrations"
    migrations.mkdir()
    (migrations / "__init__.py").write_text("", encoding="utf-8")
    (migrations / "0001_initial.py").write_text(
        """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("name", fields.CharField(max_length=50)),
            ],
            options={"table": "widget"},
        ),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    (migrations / "0002_second.py").write_text(
        """
from hare import migrations


class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = []
""".lstrip(),
        encoding="utf-8",
    )
    (migrations / "0003_third.py").write_text(
        """
from hare import migrations


class Migration(migrations.Migration):
    dependencies = [("app", "0002_second")]
    operations = []
""".lstrip(),
        encoding="utf-8",
    )


def _write_widget_chain_with_data_migration(pkg: Path) -> None:
    """Like _write_widget_chain, but 0002 is a real RunPython data migration (not a no-op) -
    for confirming squashmigrations warns about it instead of silently dropping it."""
    migrations = pkg / "migrations"
    migrations.mkdir()
    (migrations / "__init__.py").write_text("", encoding="utf-8")
    (migrations / "0001_initial.py").write_text(
        """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("name", fields.CharField(max_length=50)),
            ],
            options={"table": "widget"},
        ),
    ]
""".lstrip(),
        encoding="utf-8",
    )
    (migrations / "0002_seed_data.py").write_text(
        """
from hare import migrations
from hare.migrations import operations as ops


async def seed_widget(apps, schema_editor):
    Widget = apps.get_model("app", "Widget")
    await Widget.objects.create(id=1, name="seeded-by-runpython")


class Migration(migrations.Migration):
    dependencies = [("app", "0001_initial")]
    operations = [
        ops.RunPython(seed_widget),
    ]
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_squashmigrations_warns_about_dropped_data_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RunPython/RunSQL operations have a deliberate no-op state_forward() - the schema-diff
    squashmigrations builds its new migration from can never see them, so their effect is
    silently absent from the squashed migration with no signal at all unless this warning
    fires."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    _write_widget_chain_with_data_migration(pkg)
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(
            ["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0002", "--squashed-name", "squashed"]
        )
        assert result.exit_code == 0, result.output
        assert "0002_seed_data" in result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        squashed_source = (migrations_path / "0001_squashed.py").read_text(encoding="utf-8")
        # The data migration is carried over, its function copied from the replaced file.
        assert "RunPython(code=seed_widget)" in squashed_source
        assert "async def seed_widget(apps, schema_editor):" in squashed_source
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_squashmigrations_number_does_not_collide_with_existing_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """squashmigrations used to hardcode the new file's number to 1 regardless of the app's
    own numbering, so squashing a real "0001_initial".."0003_third" chain without deleting the
    originals first produced a migration that was ALSO leading-number "0001" - confusingly
    duplicating the app's real 0001. The number must continue the app's own sequence instead."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    _write_widget_chain(pkg)
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(
            ["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0003", "--squashed-name", "squashed"]
        )
        assert result.exit_code == 0, result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        names = sorted(p.stem for p in migrations_path.glob("*.py") if p.stem != "__init__")
        assert names == ["0001_initial", "0001_squashed", "0002_second", "0003_third"]
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_squashmigrations_migrate_on_fresh_db_succeeds_once_originals_are_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented, safe way to use a squashed migration: delete the originals it replaces,
    then migrate a fresh database. Confirms the squashed migration's own generated operations
    are correct (not just that its number/name avoid a collision)."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    _write_widget_chain(pkg)
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        }},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(
            ["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0003", "--squashed-name", "squashed"]
        )
        assert result.exit_code == 0, result.output

        migrations_path = tmp_path / "cli_app" / "migrations"
        for old_name in ("0001_initial", "0002_second", "0003_third"):
            (migrations_path / f"{old_name}.py").unlink()
        _purge_cli_app_modules()

        migrate_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert migrate_result.exit_code == 0, migrate_result.output
        assert "app.0001_squashed" in migrate_result.output

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "widget" in tables
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_squashmigrations_migrate_without_deleting_originals_runs_the_squashed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh database migrated with the replaced files still on disk runs the squashed
    migration in their place and records all of them."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    _write_widget_chain(pkg)
    db_path = tmp_path / "test.db"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        }},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        result = await _run_cli(
            ["-c", f"{module_name}.HARE_ORM", "squashmigrations", "app", "0003", "--squashed-name", "squashed"]
        )
        assert result.exit_code == 0, result.output
        _purge_cli_app_modules()

        migrate_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
        assert migrate_result.exit_code == 0, migrate_result.output
        assert "app.0001_squashed" in migrate_result.output

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            journal = {row[0] for row in conn.execute("SELECT name FROM hare_migrations")}
        finally:
            conn.close()
        assert "widget" in tables
        assert journal == {"0001_initial", "0002_second", "0003_third", "0001_squashed"}
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_sqlmigrate_requires_app_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate"])
    assert result.exit_code != 0
    assert "app_label is required" in result.output
    assert "app" in result.output


@pytest.mark.asyncio
async def test_sqlmigrate_unknown_app_label_maps_to_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "ghost", "0001_initial"])
    assert result.exit_code != 0
    assert "Unknown app label" in result.output


@pytest.mark.asyncio
async def test_sqlmigrate_on_circular_dependency_maps_to_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sqlmigrate_cmd() only caught ValueError - a genuine circular dependency raises
    ConfigurationError from sqlmigrate_api()'s own MigrationLoader.build_graph() call, which
    isn't a ValueError, so it used to leak a raw traceback instead of a clean CLIError."""
    module_name = _write_cyclic_apps_config(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cyclic_app_modules()

    try:
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "cycle_a", "0001_a"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
    finally:
        _purge_cyclic_app_modules()


@pytest.mark.asyncio
async def test_sqlmigrate_no_sql_statements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(pkg, ["0001_initial"])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "app", "0001_initial"])
    assert result.exit_code == 0, result.output
    assert "(no SQL statements)" in result.output


@pytest.mark.asyncio
async def test_sqlmigrate_prints_sql_for_real_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cli_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "app", "0001_initial"])
        assert result.exit_code == 0, result.output
        assert "widget" in result.output.lower()
        assert result.output.rstrip().endswith(";")
        # sqlite supports transactional DDL too (can_rollback_ddl=True) - a default
        # (Migration.atomic=True) migration is wrapped in BEGIN;/COMMIT; here exactly like it
        # would be for real, by MigrationExecutor.migrate()'s own atomic_migration handling.
        assert "BEGIN;" in result.output
        assert "COMMIT;" in result.output

        backward_result = await _run_cli(
            ["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "app", "0001_initial", "--backward"]
        )
        assert backward_result.exit_code == 0, backward_result.output
        assert "drop table" in backward_result.output.lower()
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_sqlmigrate_without_explicit_migrations_key_finds_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sqlmigrate_cmd used to pass the raw "migrations" config key straight to sqlmigrate_api
    with no inference fallback - an app relying on models->migrations inference (no explicit
    "migrations" key) failed with "Cannot find migration" even though it genuinely existed on
    disk."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_cli_app_modules()

    try:
        make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
        assert make_result.exit_code == 0, make_result.output
        _purge_cli_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "app", "0001_initial"])
        assert result.exit_code == 0, result.output
        assert "widget" in result.output.lower()
    finally:
        _purge_cli_app_modules()


@pytest.mark.asyncio
async def test_sqlmigrate_postgres_wraps_in_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    # +asyncpg explicit, not the bare "postgresql://" scheme's rust_pg default - this test only
    # exercises sqlmigrate's SQL-generation/transaction-wrapping logic, never a real connection,
    # but constructing the config still imports the driver's client class either way, and
    # asyncpg (unlike rust_pg's rust.pg) never needs a separate compiled-extension build step.
    "connections": {"default": "postgresql+asyncpg://user:pass@localhost:5432/db"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    for mod in list(sys.modules):
        if mod.startswith("cli_app"):
            del sys.modules[mod]

    make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
    assert make_result.exit_code == 0, make_result.output
    for mod in list(sys.modules):
        if mod.startswith("cli_app"):
            del sys.modules[mod]

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "sqlmigrate", "app", "0001_initial"])
    assert result.exit_code == 0, result.output
    assert "BEGIN;" in result.output
    assert "COMMIT;" in result.output


@pytest.mark.asyncio
async def test_version_flag_exits_zero() -> None:
    result = await _run_cli(["-V"])
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_shell_no_provider_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module.ShellLauncher, "interactive_shell_class", None)

    result = await _run_cli(["shell"])
    assert result.exit_code == 1
    assert "hare shell needs IPython" in result.output


@pytest.mark.asyncio
async def test_makemigrations_empty_writes_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(pkg, ["0001_initial", "0002_second"])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FixedAutodetector(MigrationAutodetector):
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, apps, apps_config, **_kwargs) -> None:
            super().__init__(apps, apps_config, now=lambda: dt.datetime(2024, 1, 2, 3, 4))

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FixedAutodetector)

    with _override_hare_apps({"app": {}}):
        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "--empty", "app"])
        assert result.exit_code == 0

        migrations_path = tmp_path / "cli_app" / "migrations"
        migration_file = migrations_path / "0003_auto_20240102_0304.py"
        assert migration_file.exists()
        content = migration_file.read_text(encoding="utf-8")
        assert "operations = [" in content
        assert 'dependencies = [("app", "0001_initial"), ("app", "0002_second")]' in content


@pytest.mark.asyncio
async def test_makemigrations_empty_respects_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "cli_app")
    _write_migrations(pkg, ["0001_initial", "0002_second"])
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {
            "models": ["cli_app.models"],
            "default_connection": "default",
            "migrations": "cli_app.migrations",
        },
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def fake_init(**_kwargs) -> None:
        return None

    class FixedAutodetector(MigrationAutodetector):
        warnings: list[str] = []  # noqa: RUF012

        data_loss_warnings: list[str] = []  # noqa: RUF012

        project_state = None

        def __init__(self, apps, apps_config, **_kwargs) -> None:
            super().__init__(apps, apps_config, now=lambda: dt.datetime(2024, 1, 2, 3, 4))

    monkeypatch.setattr(cli_module.Hare, "init", fake_init)
    monkeypatch.setattr(migration_maker, "MigrationAutodetector", FixedAutodetector)

    with _override_hare_apps({"app": {}}):
        result = await _run_cli(
            [
                "-c",
                f"{module_name}.HARE_ORM",
                "makemigrations",
                "--empty",
                "--name",
                "manual",
                "app",
            ]
        )
        assert result.exit_code == 0

        migrations_path = tmp_path / "cli_app" / "migrations"
        assert (migrations_path / "0003_manual.py").exists()


async def _create_inspectdb_schema(tmp_path: Path, db_path: Path) -> None:
    _write_package(tmp_path, "cli_inspectdb_app")
    (tmp_path / "cli_inspectdb_app" / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    description = fields.TextField(null=True)


class Gadget(Model):
    id = fields.IntField(primary_key=True)
    widget = fields.ForeignKeyField("app.Widget", related_name="gadgets")
""".lstrip(),
        encoding="utf-8",
    )

    async with HareContext() as setup_ctx:
        await setup_ctx.init(
            HareConfig.from_db_url(f"sqlite+aiosqlite:///{db_path}", {"app": ["cli_inspectdb_app.models"]})
        )
        await setup_ctx.generate_schemas(safe=True)


def _write_inspectdb_settings(tmp_path: Path, db_path: Path) -> str:
    return _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_inspectdb_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )


@pytest.mark.asyncio
async def test_inspectdb_prints_generated_model_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    db_path = tmp_path / "inspectdb_cli.sqlite"
    await _create_inspectdb_schema(tmp_path, db_path)
    module_name = _write_inspectdb_settings(tmp_path, db_path)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "inspectdb"])

    assert result.exit_code == 0
    assert "class Widget(Model):" in result.output
    assert "class Gadget(Model):" in result.output
    assert "primary_key=True" in result.output
    assert "ForeignKeyField" in result.output
    assert "models.Widget" in result.output


@pytest.mark.asyncio
async def test_inspectdb_specific_table_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    db_path = tmp_path / "inspectdb_cli_single.sqlite"
    await _create_inspectdb_schema(tmp_path, db_path)
    module_name = _write_inspectdb_settings(tmp_path, db_path)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "inspectdb", "widget"])

    assert result.exit_code == 0
    assert "class Widget(Model):" in result.output
    assert "class Gadget(Model):" not in result.output


@pytest.mark.asyncio
async def test_inspectdb_unknown_connection_maps_to_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd --connection used to fall straight through to Connections.get(), an unhandled
    lookup error rather than the same clear, exit-code-2 CLIUsageError sqlmigrate/migrate
    already give for an unknown app label."""
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    db_path = tmp_path / "inspectdb_cli_unknown_conn.sqlite"
    await _create_inspectdb_schema(tmp_path, db_path)
    module_name = _write_inspectdb_settings(tmp_path, db_path)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "inspectdb", "--connection", "ghost"])

    # This test has been intermittently flaky under the full parallel (-n auto) suite - always
    # passes in isolation, never reproduced across 90+ targeted reruns. The diagnostics below (not
    # otherwise visible from result.exit_code/result.output alone) capture the state most likely
    # to explain a stale-import/module-shadowing race if this fires again: which cli_*-prefixed
    # modules the process already had cached going into this test, whether this test's own
    # dynamically-written settings module ended up first on sys.path, and whether the file it
    # wrote is actually the one that got imported.
    diagnostics = (
        f"module_name={module_name!r} exit_code={result.exit_code} output={result.output!r} "
        f"sys.path[:3]={sys.path[:3]!r} "
        f"cli_-prefixed sys.modules={sorted(name for name in sys.modules if name.startswith('cli_'))!r} "
        f"settings_file_exists={(tmp_path / f'{module_name}.py').exists()!r} "
        f"imported_module_file={getattr(sys.modules.get(module_name), '__file__', 'NOT IMPORTED')!r}"
    )
    assert result.exit_code != 0, diagnostics
    assert "Unknown connection" in result.output, diagnostics


@pytest.mark.asyncio
async def test_migrate_unreachable_postgres_maps_to_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable connection used to let hare.exceptions.DBConnectionError escape all the way
    to the user as a raw, multi-frame traceback instead of a clean CLIError."""
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "postgresql://user:pass@0.0.0.0:1/somedb"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])

    assert result.exit_code != 0
    assert "Could not connect to the database" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_database_error_boundary_maps_configuration_error_to_cli_error() -> None:
    """A connection attempt made AFTER app init already succeeded (e.g. distributed-recover
    lazily connecting to a participant alias) can still fail its own auth/config check on that
    alias - a bad password/cert maps to ConfigurationError, not DBConnectionError. This used to
    escape database_error_boundary() as a raw traceback instead of a clean CLIError, since
    hare_cli_context()'s own ConfigurationError catch only wraps ctx.init() itself."""
    with pytest.raises(CLIError, match="Authentication failed connecting to somedb"):
        async with CommandContext.database_error_boundary():
            raise ConfigurationError("Authentication failed connecting to somedb as user 'postgres'")


@pytest.mark.asyncio
async def test_database_error_boundary_does_not_mislabel_a_non_connectivity_configuration_error() -> None:
    """ConfigurationError is hare-orm's general "something about this setup/operation is wrong"
    exception - raised for plenty of reasons that have nothing to do with connectivity at all
    (an unknown model option, an unknown app label, ...). Prefixing every one of those with
    "Could not connect to the database"
    sent the user chasing a network/credentials problem that doesn't exist - the exact mistake
    the sibling OperationalError branch already exists to avoid for a different exception type."""
    with pytest.raises(CLIError, match="has no primary key at all") as exc_info:
        async with CommandContext.database_error_boundary():
            raise ConfigurationError("inspectdb: table 'orphaned' has no primary key at all - ...")
    assert "Could not connect to the database" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_migrate_operational_error_maps_to_its_own_cli_error_not_connectivity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """database_error_boundary() used to label EVERY OperationalError (a real SQL execution
    failure against a perfectly reachable connection - e.g. "table already exists", the shape
    left behind by squashmigrations when the old migration files aren't removed and the DB was
    already migrated through them individually) the exact same way as a genuine DBConnectionError
    ("Could not connect to the database") - sending the user chasing a network/credentials
    problem that doesn't exist. Forces a real OperationalError by dropping the migration
    tracking table (hare_migrations) out from under an already-migrated DB, then re-running
    migrate - the CREATE TABLE for the already-existing "widget" table then genuinely fails."""
    pkg = _write_package(tmp_path, "cli_app")
    (pkg / "models.py").write_text(
        "from hare import fields\nfrom hare.models import Model\n\n\n"
        "class Widget(Model):\n    id = fields.IntField(primary_key=True)\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "db.sqlite3"
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite://{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert (await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])).exit_code == 0
    assert (await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])).exit_code == 0

    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("DROP TABLE IF EXISTS hare_migrations")
        await conn.commit()

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])

    assert result.exit_code != 0
    assert "Database operation failed" in result.output
    assert "Could not connect to the database" not in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_makemigrations_broken_model_module_maps_to_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model file that raises at import time (typo, bad reference) used to let the raw
    NameError escape all the way to the user as a traceback instead of a clean CLIError."""
    pkg = _write_package(tmp_path, "cli_broken_app")
    (pkg / "models.py").write_text(
        "from hare import fields\nfrom hare.models import Model\n\n\nclass Thing(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    broken = this_name_does_not_exist_anywhere\n",
        encoding="utf-8",
    )
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_broken_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_broken_app", None)
    sys.modules.pop("cli_broken_app.models", None)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations"])

    assert result.exit_code != 0
    assert "cli_broken_app.models" in result.output
    assert "this_name_does_not_exist_anywhere" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_makemigrations_configuration_error_inside_context_maps_to_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ConfigurationError raised from code running AFTER hare_cli_context()'s own
    ctx.init() (e.g. MigrationGraph.get_single_leaf() finding a forked migration history with no
    merge yet) used to escape hare_cli_context()'s try/except entirely - that only wraps
    ctx.init() itself, not the rest of the `async with` body - leaking a raw traceback instead of
    a clean CLIError. Simulates it directly via a monkeypatched MigrationAutodetector.changes()
    rather than depending on the exact internal call graph that can trigger a ConfigurationError
    there today."""
    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "app": {"models": ["cli_app.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("cli_app", None)
    sys.modules.pop("cli_app.migrations", None)

    async def broken_changes(self) -> list[MigrationWriter]:
        raise ConfigurationError("app 'app' has multiple migration heads with no merge migration")

    monkeypatch.setattr(MigrationAutodetector, "changes", broken_changes)

    result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations"])

    assert result.exit_code != 0
    assert "multiple migration heads" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_config_file_malformed_json_maps_to_cli_error(tmp_path: Path) -> None:
    """A malformed config file used to let the raw json.JSONDecodeError escape as a traceback
    instead of a clean CLIError."""
    config_file = tmp_path / "hare.json"
    config_file.write_text('{"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {', encoding="utf-8")

    result = await _run_cli(["-c", str(config_file), "history"])

    assert result.exit_code != 0
    assert "Invalid Hare ORM configuration" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_config_file_malformed_yaml_maps_to_cli_error(tmp_path: Path) -> None:
    """Same as above, for a malformed YAML config file."""
    config_file = tmp_path / "hare.yaml"
    yaml_content = "connections:\n  default: sqlite+aiosqlite://:memory:\napps:\n  app:\n  models: bad\n"
    config_file.write_text(yaml_content, encoding="utf-8")

    result = await _run_cli(["-c", str(config_file), "history"])

    assert result.exit_code != 0
    assert "Invalid Hare ORM configuration" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_config_file_that_is_a_directory_maps_to_cli_error(tmp_path: Path) -> None:
    """from_config_file() only caught FileNotFoundError - a directory (or any other unreadable
    path) raised a raw PermissionError/IsADirectoryError traceback instead of a clean CLIError."""
    config_directory = tmp_path / "hare.json"
    config_directory.mkdir()

    result = await _run_cli(["-c", str(config_directory), "history"])

    assert result.exit_code != 0
    assert "Cannot read config file" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_config_file_that_is_not_utf8_maps_to_cli_error(tmp_path: Path) -> None:
    """The config file used to be opened with the platform's default encoding (cp1251 on
    Windows) instead of UTF-8, and a genuinely undecodable file raised a raw
    UnicodeDecodeError traceback."""
    config_file = tmp_path / "hare.json"
    config_file.write_bytes(b'{"connections": "\xff\xfe"}')

    result = await _run_cli(["-c", str(config_file), "history"])

    assert result.exit_code != 0
    assert "not valid UTF-8" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_config_file_with_non_ascii_content_is_read_as_utf8(tmp_path: Path) -> None:
    config_file = tmp_path / "hare.json"
    config_file.write_text(
        '{"connections": {"default": "sqlite+aiosqlite://:memory:"}, "note": "привет"}', encoding="utf-8"
    )

    result = await _run_cli(["-c", str(config_file), "history"])

    assert "UTF-8" not in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_malformed_pyproject_toml_maps_to_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigLoader.locate() let a malformed pyproject.toml's tomllib.TOMLDecodeError escape as a
    raw traceback instead of a clean CLIError."""
    monkeypatch.delenv("HARE_ORM", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("x = \n", encoding="utf-8")

    result = await _run_cli(["heads"])

    assert result.exit_code != 0
    assert "Cannot read pyproject.toml" in result.output
    assert "Traceback (most recent call last)" not in result.output


@pytest.mark.asyncio
async def test_pyproject_toml_hare_orm_of_wrong_type_maps_to_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[tool.hare] hare_orm = 5 used to reach ConfigLoader.load() as an int and crash with a raw
    AttributeError ('int' object has no attribute 'strip')."""
    monkeypatch.delenv("HARE_ORM", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.hare]\nhare_orm = 5\n", encoding="utf-8")

    result = await _run_cli(["heads"])

    assert result.exit_code != 0
    assert "must be a string" in result.output
    assert "Traceback (most recent call last)" not in result.output


# =============================================================================
# distributed-recover
# =============================================================================


def _distributed_recover_test_pg_template() -> str | None:
    """Mirrors tests/test_transactions_distributed.py's own HARE_TEST_DB check - None (skip)
    unless it's a Postgres URL."""
    raw_db_url = os.environ.get("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        return None
    return raw_db_url.replace("\\{", "{").replace("\\}", "}")


def _fresh_pg_url(template: str) -> str:
    return template.format(uuid.uuid4().hex) if "{}" in template else f"{template}_{uuid.uuid4().hex}"


@pytest_asyncio.fixture
async def distributed_recover_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SimpleNamespace]:
    """A real coordinator + participant Postgres database, wired into a HARE_ORM settings module
    on disk (for `_run_cli()`), plus the SAME two connections opened in a live HareContext so a
    test can hand-craft a stuck distributed() scenario (a decision row, a PREPARE TRANSACTION)
    before invoking the CLI against it. Skips cleanly off Postgres or with
    max_prepared_transactions=0, same as tests/test_transactions_distributed.py."""
    template = _distributed_recover_test_pg_template()
    if template is None:
        pytest.skip("distributed-recover CLI tests need a Postgres HARE_TEST_DB")

    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{
        "coordinator": {_fresh_pg_url(template)!r},
        "participant": {_fresh_pg_url(template)!r},
    }},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "coordinator"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    hare_orm_module = importlib.import_module(module_name)
    async with HareContext() as ctx:
        await ctx.init(config=hare_orm_module.HARE_ORM, _create_db=True)
        coordinator = Connections.get("coordinator")
        if not coordinator.features.supports_two_phase_commit:
            pytest.skip("Coordinator connection doesn't support distributed transactions")
        _, rows = await coordinator.execute("SHOW max_prepared_transactions")
        if int(rows[0]["max_prepared_transactions"]) == 0:
            pytest.skip("Postgres has max_prepared_transactions=0 - PREPARE TRANSACTION disabled")
        try:
            yield SimpleNamespace(hare_orm_arg=f"{module_name}.HARE_ORM", ctx=ctx)
        finally:
            await ctx.connections.close_all(discard=False)
            for conn in ctx.connections.all():
                attempts = 20
                for attempt in range(attempts):
                    try:
                        await conn.db_delete()
                        break
                    except OperationalError:
                        if attempt == attempts - 1:
                            raise
                        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_distributed_recover_reports_then_resolves_a_committed_decision(distributed_recover_setup) -> None:
    """Simulates a crash right after Transactions.distributed()'s coordinator commit but before
    the participant's own COMMIT PREPARED landed - `hare distributed-recover` must find it
    (report-only), then actually finish delivering it with --finish."""
    xid = f"hare_dtx_{uuid.uuid4().hex}"
    gid = DistributedCoordinator._participant_gid(xid, "participant")
    coordinator = Connections.get("coordinator")
    participant = Connections.get("participant")

    await DistributedCoordinator._ensure_distributed_decisions_table(coordinator)
    await coordinator.execute(
        "INSERT INTO hare_distributed_decisions (xid, coordinator_alias, participant_aliases) VALUES ($1, $2, $3)",
        [xid, "coordinator", "participant"],
    )
    participant_tx = participant._in_transaction()
    participant_client = await participant_tx.__aenter__()
    await participant_client.execute(f"PREPARE TRANSACTION '{gid}'")
    participant_client._finalized = True
    await participant_tx.__aexit__(None, None, None)

    report = await _run_cli(
        [
            "-c",
            distributed_recover_setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--older-than",
            "0",
        ]
    )
    assert report.exit_code == 1
    assert xid in report.output
    assert "participant" in report.output
    assert "COMMIT" in report.output.upper()

    resolve = await _run_cli(
        [
            "-c",
            distributed_recover_setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--finish",
            "--older-than",
            "0",
        ]
    )
    assert resolve.exit_code == 0

    _, prepared_rows = await participant.execute("SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1", [gid])
    assert prepared_rows == []
    _, decision_rows = await coordinator.execute(
        "SELECT resolved_at FROM hare_distributed_decisions WHERE xid = $1", [xid]
    )
    assert decision_rows[0]["resolved_at"] is not None


@pytest.mark.asyncio
async def test_distributed_recover_orphaned_prepared_transaction_gets_rolled_back(distributed_recover_setup) -> None:
    """No decision row anywhere for this xid - it never really committed (presumed abort) -
    --finish must ROLLBACK PREPARED it, not COMMIT PREPARED.

    The xid must be built via DistributedCoordinator._make_xid() (embedding the coordinator alias), not an
    ad hoc "hare_dtx_<uuid>" literal - the orphan-scan path (unlike the decision-row path the
    other tests in this module exercise) runs every candidate through
    DistributedCoordinator._xid_belongs_to_coordinator(), which only matches the real
    "hare_dtx_<coordinator>:<uuid>" shape."""
    xid = DistributedCoordinator._make_xid("coordinator")
    gid = DistributedCoordinator._participant_gid(xid, "participant")
    participant = Connections.get("participant")

    participant_tx = participant._in_transaction()
    participant_client = await participant_tx.__aenter__()
    await participant_client.execute(f"PREPARE TRANSACTION '{gid}'")
    participant_client._finalized = True
    await participant_tx.__aexit__(None, None, None)

    report = await _run_cli(
        [
            "-c",
            distributed_recover_setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--older-than",
            "0",
        ]
    )
    assert report.exit_code == 1
    assert "ROLLBACK" in report.output.upper()

    resolve = await _run_cli(
        [
            "-c",
            distributed_recover_setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--finish",
            "--older-than",
            "0",
        ]
    )
    assert resolve.exit_code == 0

    _, prepared_rows = await participant.execute("SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1", [gid])
    assert prepared_rows == []


@pytest.mark.asyncio
async def test_distributed_recover_ignores_an_orphan_of_a_coordinator_sharing_the_alias_prefix(
    distributed_recover_setup,
) -> None:
    """A prepared transaction of coordinator "coordinator:other" isn't this coordinator's orphan,
    though its xid starts with "hare_dtx_coordinator:"."""
    xid = DistributedCoordinator._make_xid("coordinator:other")
    gid = DistributedCoordinator._participant_gid(xid, "participant")
    participant = Connections.get("participant")

    participant_tx = participant._in_transaction()
    participant_client = await participant_tx.__aenter__()
    await participant_client.execute(f"PREPARE TRANSACTION '{gid}'")
    participant_client._finalized = True
    await participant_tx.__aexit__(None, None, None)
    try:
        stale = await DistributedCoordinator.detect_stale_prepared_transactions("coordinator", older_than_seconds=0)
        assert [entry.xid for entry in stale] == []
    finally:
        await participant.execute(f"ROLLBACK PREPARED '{gid}'")


def test_participant_gid_like_pattern_matches_the_alias_literally() -> None:
    assert DistributedCoordinator._get_participant_gid_like_pattern("db_1%", "\\") == "hare\\_dtx\\_%:db\\_1\\%"


def test_xid_belongs_only_to_its_exact_coordinator() -> None:
    nested_coordinator_xid = DistributedCoordinator._make_xid("a:b")
    assert DistributedCoordinator._xid_belongs_to_coordinator(nested_coordinator_xid, "a:b")
    assert not DistributedCoordinator._xid_belongs_to_coordinator(nested_coordinator_xid, "a")
    gid_of_alias_with_colon = DistributedCoordinator._participant_gid(DistributedCoordinator._make_xid("c"), "x:db")
    assert not DistributedCoordinator._xid_belongs_to_coordinator(gid_of_alias_with_colon.removesuffix(":db"), "c")


@pytest.mark.asyncio
async def test_distributed_recover_older_than_filters_out_recent_entries(distributed_recover_setup) -> None:
    """A decision/prepared transaction younger than --older-than may still be mid-flight - left
    alone by default rather than resolved prematurely."""
    xid = f"hare_dtx_{uuid.uuid4().hex}"
    gid = DistributedCoordinator._participant_gid(xid, "participant")
    participant = Connections.get("participant")

    participant_tx = participant._in_transaction()
    participant_client = await participant_tx.__aenter__()
    await participant_client.execute(f"PREPARE TRANSACTION '{gid}'")
    participant_client._finalized = True
    await participant_tx.__aexit__(None, None, None)

    report = await _run_cli(
        [
            "-c",
            distributed_recover_setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--older-than",
            "3600",
        ]
    )
    assert report.exit_code == 0
    assert "No stale distributed transactions found" in report.output

    await participant.execute(f"ROLLBACK PREPARED '{gid}'")


@pytest.mark.asyncio
async def test_distributed_recover_handles_a_participant_alias_no_longer_configured(
    distributed_recover_setup,
) -> None:
    """A decision row records whatever participant aliases were configured when the original
    distributed() call ran - config can drift (a rename/removal) before recovery runs, since
    older_than_seconds exists specifically to allow that gap. Both the detection scan
    (DistributedCoordinator.detect_stale_prepared_transactions) and the --finish execution loop used to
    call Connections.get()/_get_connection() for that alias unguarded, so an unknown alias raised
    a raw, uncaught ConfigurationError that crashed the whole run - hiding every OTHER stale
    entry from the report, not just the one with the missing alias.

    Simulated by running `distributed-recover` against a SECOND config that only declares
    "coordinator" - a genuinely removed alias, unlike monkeypatching ConnectionHandler.get()
    directly, which would also break the CLI's own eager connection setup for every OTHER
    command reading this same config (HareCLI.hare_cli_context() validates every configured
    alias at startup, not just the ones distributed-recover itself touches)."""
    xid = f"hare_dtx_{uuid.uuid4().hex}"
    gid = DistributedCoordinator._participant_gid(xid, "participant")
    coordinator = Connections.get("coordinator")
    participant = Connections.get("participant")

    await DistributedCoordinator._ensure_distributed_decisions_table(coordinator)
    await coordinator.execute(
        "INSERT INTO hare_distributed_decisions (xid, coordinator_alias, participant_aliases) VALUES ($1, $2, $3)",
        [xid, "coordinator", "participant"],
    )
    participant_tx = participant._in_transaction()
    participant_client = await participant_tx.__aenter__()
    await participant_client.execute(f"PREPARE TRANSACTION '{gid}'")
    participant_client._finalized = True
    await participant_tx.__aexit__(None, None, None)

    module_name = distributed_recover_setup.hare_orm_arg.removesuffix(".HARE_ORM")
    hare_orm_module = importlib.import_module(module_name)
    coordinator_url = hare_orm_module.HARE_ORM["connections"]["coordinator"]
    settings_dir = Path(hare_orm_module.__file__).parent
    coordinator_only_module_name = _write_settings(
        settings_dir,
        f"""
HARE_ORM = {{
    "connections": {{
        "coordinator": {coordinator_url!r},
    }},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "coordinator"}},
    }},
}}
""".lstrip(),
        f"{module_name}_coordinator_only",
    )
    importlib.invalidate_caches()
    coordinator_only_arg = f"{coordinator_only_module_name}.HARE_ORM"

    report = await _run_cli(
        [
            "-c",
            coordinator_only_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--older-than",
            "0",
        ]
    )
    assert report.exit_code == 1
    assert "Traceback" not in report.output
    assert xid in report.output
    assert "no longer configured" in report.output

    resolve = await _run_cli(
        [
            "-c",
            coordinator_only_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--finish",
            "--older-than",
            "0",
        ]
    )
    assert resolve.exit_code == 1
    assert "Traceback" not in resolve.output
    assert "failed" in resolve.output.lower()

    _, decision_rows = await coordinator.execute(
        "SELECT resolved_at FROM hare_distributed_decisions WHERE xid = $1", [xid]
    )
    assert decision_rows[0]["resolved_at"] is None

    await participant.execute(f"ROLLBACK PREPARED '{gid}'")


@pytest_asyncio.fixture
async def distributed_recover_multi_participant_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SimpleNamespace]:
    """Same as distributed_recover_setup, but with TWO participants sharing one coordinator -
    needed to test resolution grouped correctly BY XID (Transactions.distributed()'s real shape
    for 2+ participants), which the single-participant fixture above can't exercise."""
    template = _distributed_recover_test_pg_template()
    if template is None:
        pytest.skip("distributed-recover CLI tests need a Postgres HARE_TEST_DB")

    _write_package(tmp_path, "cli_app")
    module_name = _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{
        "coordinator": {_fresh_pg_url(template)!r},
        "participant_a": {_fresh_pg_url(template)!r},
        "participant_b": {_fresh_pg_url(template)!r},
    }},
    "apps": {{
        "app": {{"models": ["cli_app.models"], "default_connection": "coordinator"}},
    }},
}}
""".lstrip(),
        f"cli_settings_{tmp_path.name}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    hare_orm_module = importlib.import_module(module_name)
    async with HareContext() as ctx:
        await ctx.init(config=hare_orm_module.HARE_ORM, _create_db=True)
        coordinator = Connections.get("coordinator")
        if not coordinator.features.supports_two_phase_commit:
            pytest.skip("Coordinator connection doesn't support distributed transactions")
        _, rows = await coordinator.execute("SHOW max_prepared_transactions")
        if int(rows[0]["max_prepared_transactions"]) == 0:
            pytest.skip("Postgres has max_prepared_transactions=0 - PREPARE TRANSACTION disabled")
        try:
            yield SimpleNamespace(hare_orm_arg=f"{module_name}.HARE_ORM", ctx=ctx)
        finally:
            await ctx.connections.close_all(discard=False)
            for conn in ctx.connections.all():
                attempts = 20
                for attempt in range(attempts):
                    try:
                        await conn.db_delete()
                        break
                    except OperationalError:
                        if attempt == attempts - 1:
                            raise
                        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_distributed_recover_partial_resolve_does_not_mark_whole_xid_resolved(
    distributed_recover_multi_participant_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One decision, TWO participants - if COMMIT PREPARED lands for participant_a but fails for
    participant_b, --finish must NOT mark the whole xid's resolved_at. Doing so would make a
    LATER run misclassify participant_b as an orphan with no decision row (presumed abort) and
    wrongly ROLLBACK PREPARED a write that already durably committed on the coordinator."""
    setup = distributed_recover_multi_participant_setup
    xid = f"hare_dtx_{uuid.uuid4().hex}"
    gid_a = DistributedCoordinator._participant_gid(xid, "participant_a")
    gid_b = DistributedCoordinator._participant_gid(xid, "participant_b")
    coordinator = Connections.get("coordinator")
    participant_a = Connections.get("participant_a")
    participant_b = Connections.get("participant_b")

    await DistributedCoordinator._ensure_distributed_decisions_table(coordinator)
    await coordinator.execute(
        "INSERT INTO hare_distributed_decisions (xid, coordinator_alias, participant_aliases) VALUES ($1, $2, $3)",
        [xid, "coordinator", "participant_a,participant_b"],
    )
    for connection, gid in ((participant_a, gid_a), (participant_b, gid_b)):
        tx = connection._in_transaction()
        client = await tx.__aenter__()
        await client.execute(f"PREPARE TRANSACTION '{gid}'")
        client._finalized = True
        await tx.__aexit__(None, None, None)

    real_commit_prepared = DistributedCoordinator.commit_prepared

    async def flaky_commit_prepared(client, alias: str, xid_arg: str) -> None:
        if alias == "participant_b":
            raise ConnectionError("simulated commit-prepared failure")
        await real_commit_prepared(client, alias, xid_arg)

    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", flaky_commit_prepared)
    try:
        resolve = await _run_cli(
            [
                "-c",
                setup.hare_orm_arg,
                "distributed-recover",
                "--coordinator",
                "coordinator",
                "--finish",
                "--older-than",
                "0",
            ]
        )
    finally:
        monkeypatch.setattr(DistributedCoordinator, "commit_prepared", real_commit_prepared)

    assert resolve.exit_code == 1

    _, decision_rows = await coordinator.execute(
        "SELECT resolved_at FROM hare_distributed_decisions WHERE xid = $1", [xid]
    )
    assert decision_rows[0]["resolved_at"] is None, (
        "resolved_at must stay NULL while participant_b is still unresolved - marking the whole "
        "xid resolved here would hide it from every later distributed-recover run"
    )

    # participant_a really did get COMMIT PREPARED'd (not mocked) - it's gone from pg_prepared_xacts.
    _, prepared_a = await participant_a.execute("SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1", [gid_a])
    assert prepared_a == []
    # participant_b's prepared transaction is still there - the mocked commit_prepared() never
    # ran any real SQL against it.
    _, prepared_b = await participant_b.execute("SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1", [gid_b])
    assert len(prepared_b) == 1

    # A second run (real commit_prepared restored) must still find participant_b needing
    # COMMIT PREPARED - exactly the case the finished_xids grouping bug would have hidden.
    second_resolve = await _run_cli(
        [
            "-c",
            setup.hare_orm_arg,
            "distributed-recover",
            "--coordinator",
            "coordinator",
            "--finish",
            "--older-than",
            "0",
        ]
    )
    assert second_resolve.exit_code == 0

    _, prepared_b_after = await participant_b.execute(
        "SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE gid = $1", [gid_b]
    )
    assert prepared_b_after == []
    _, decision_rows_after = await coordinator.execute(
        "SELECT resolved_at FROM hare_distributed_decisions WHERE xid = $1", [xid]
    )
    assert decision_rows_after[0]["resolved_at"] is not None


def test_configure_output_encoding_makes_redirected_stdout_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251", newline="\n")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    cli_module.HareCLI.configure_output_encoding()
    print("\u00dc \u2192 \u2713 \u043f\u0440\u0438\u0432\u0435\u0442")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "\u00dc \u2192 \u2713 \u043f\u0440\u0438\u0432\u0435\u0442\n"


def test_configure_output_encoding_redirected_to_file_in_subprocess(tmp_path: Path) -> None:
    import subprocess

    output_path = tmp_path / "out.txt"
    environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    code = (
        "from hare.cli.hare_cli import HareCLI\nHareCLI.configure_output_encoding()\n"
        "print('\\u00dc \\u2192 \\u2713')\n"
    )
    with output_path.open("wb") as output_handle:
        completed = subprocess.run([sys.executable, "-c", code], stdout=output_handle, env=environment, check=False)

    assert completed.returncode == 0
    assert output_path.read_bytes().decode("utf-8").strip() == "\u00dc \u2192 \u2713"
