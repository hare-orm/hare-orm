from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from hare.cli import hare_cli as cli_module
from hare.cli.arg_parser_builder import ArgParserBuilder
from tests.utils.cli_commands import CliCommands
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.sqlite_only

BLOG_MODELS = """
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
{extra}"""

SHOP_MODELS = """
from hare import fields
from hare.models import Model


class Product(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("blog.Author", related_name="products")
"""


def _table_names(database_path: Path) -> set[str]:
    connection = sqlite3.connect(database_path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _bio_column_exists(database_path: Path) -> bool:
    connection = sqlite3.connect(database_path)
    try:
        return any(row[1] == "bio" for row in connection.execute("PRAGMA table_info(author)"))
    finally:
        connection.close()


def _latest_blog_migration_name(project_root: Path) -> str:
    return sorted(path.stem for path in (project_root / "scope_blog" / "migrations").glob("0*.py"))[-1]


async def _build_blog_shop_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shop_depends_on_latest: bool):
    blog_package = ProjectSources(
        tmp_path,
    ).write_package("scope_blog", BLOG_MODELS.format(extra=""))
    ProjectSources(
        tmp_path,
    ).write_package("scope_shop", SHOP_MODELS)
    database_path = tmp_path / "scope.sqlite3"
    settings_name = f"scope_settings_{tmp_path.name}"
    (tmp_path / f"{settings_name}.py").write_text(
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite://{database_path.as_posix()}"}},
    "apps": {{
        "blog": {{"models": ["scope_blog.models"], "default_connection": "default"}},
        "shop": {{"models": ["scope_shop.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    config_argument = f"{settings_name}.HARE_ORM"

    async def make_migrations(*app_labels: str) -> None:
        importlib.invalidate_caches()
        assert (
            await CliCommands.run(["-c", config_argument, "makemigrations", *app_labels], project=tmp_path)
        ).exit_code == 0

    def add_blog_field() -> None:
        blog_models_with_bio = BLOG_MODELS.format(extra="    bio = fields.TextField(null=True)\n")
        (blog_package / "models.py").write_text(blog_models_with_bio, encoding="utf-8")

    if shop_depends_on_latest:
        await make_migrations("blog")
        add_blog_field()
        await make_migrations("blog")
        await make_migrations("shop")
    else:
        await make_migrations()
        add_blog_field()
        await make_migrations("blog")
    assert (await CliCommands.run(["-c", config_argument, "migrate"], project=tmp_path)).exit_code == 0
    assert {"author", "product"} <= _table_names(database_path)
    return SimpleNamespace(config_argument=config_argument, database_path=database_path, root=tmp_path)


@pytest_asyncio.fixture
async def blog_shop_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """blog has 0001 and 0002; shop.0001 depends on blog.0002 (blog's latest); all applied."""
    yield await _build_blog_shop_project(tmp_path, monkeypatch, shop_depends_on_latest=True)


@pytest_asyncio.fixture
async def blog_shop_project_on_first_blog_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """blog has 0001 and 0002; shop.0001 depends only on blog.0001; all applied."""
    yield await _build_blog_shop_project(tmp_path, monkeypatch, shop_depends_on_latest=False)


@pytest.mark.asyncio
async def test_migrate_to_latest_app_migration_is_noop_for_other_apps(blog_shop_project, tmp_path) -> None:
    """`migrate blog <latest>` used to plan a ROLLBACK of every other app's migration depending
    on blog (shop.0001), dropping shop's table."""
    project = blog_shop_project
    latest_name = _latest_blog_migration_name(project.root)

    result = await CliCommands.run(["-c", project.config_argument, "migrate", "blog", latest_name], project=tmp_path)

    assert result.exit_code == 0, result.output
    assert "ROLLBACK" not in result.output
    assert "product" in _table_names(project.database_path)


@pytest.mark.asyncio
async def test_migrate_to_earlier_app_migration_rolls_back_dependents_of_rolled_back_migrations(
    blog_shop_project, tmp_path
) -> None:
    """shop.0001 depends on blog.0002, so unapplying blog.0002 has to unapply it first."""
    project = blog_shop_project
    result = await CliCommands.run(
        ["-c", project.config_argument, "migrate", "blog", "0001_initial"], project=tmp_path
    )

    assert result.exit_code == 0, result.output
    assert "ROLLBACK  shop.0001_initial" in result.output
    assert "ROLLBACK  blog.0002" in result.output
    assert result.output.index("shop.0001_initial") < result.output.index("blog.0002")
    assert not _bio_column_exists(project.database_path)
    assert "author" in _table_names(project.database_path)
    assert "product" not in _table_names(project.database_path)


@pytest.mark.asyncio
async def test_migrate_to_earlier_app_migration_keeps_apps_depending_only_on_target(
    blog_shop_project_on_first_blog_migration, tmp_path
) -> None:
    project = blog_shop_project_on_first_blog_migration
    result = await CliCommands.run(
        ["-c", project.config_argument, "migrate", "blog", "0001_initial"], project=tmp_path
    )

    assert result.exit_code == 0, result.output
    assert "ROLLBACK  blog.0002" in result.output
    assert "shop." not in result.output
    assert "product" in _table_names(project.database_path)
    assert not _bio_column_exists(project.database_path)


@pytest.mark.asyncio
async def test_migrate_app_to_zero_rolls_back_dependent_apps(blog_shop_project, tmp_path) -> None:
    project = blog_shop_project
    result = await CliCommands.run(["-c", project.config_argument, "migrate", "blog", "zero"], project=tmp_path)

    assert result.exit_code == 0, result.output
    assert "ROLLBACK  shop.0001_initial" in result.output
    tables = _table_names(project.database_path)
    assert "product" not in tables
    assert "author" not in tables


def test_history_for_app_with_cross_app_foreign_key_lists_only_selected_app(blog_shop_project) -> None:
    """`history shop` built its config from the selected app alone, so shop's FK to blog.Author
    failed to resolve ("No app with name 'blog' registered"). Runs in a fresh interpreter: models
    already initialised by an earlier in-process command keep their resolved relations and hide it."""
    project = blog_shop_project
    completed = subprocess.run(
        [sys.executable, "-m", "hare.cli", "-c", project.config_argument, "history", "shop"],
        cwd=project.root,
        env={**os.environ, "PYTHONPATH": str(project.root)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "shop 0001_initial" in completed.stdout
    assert "blog" not in completed.stdout


@pytest.mark.asyncio
async def test_makemigrations_keeps_unicode_migration_name(
    blog_shop_project, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`--name "добавить поле"` used to collapse to `0003_auto` (every non-ASCII character stripped)."""
    project = blog_shop_project
    blog_models_path = project.root / "scope_blog" / "models.py"
    blog_models_path.write_text(
        BLOG_MODELS.format(extra="    bio = fields.TextField(null=True)\n    nick = fields.TextField(null=True)\n"),
        encoding="utf-8",
    )
    importlib.invalidate_caches()

    result = await CliCommands.run(
        ["-c", project.config_argument, "makemigrations", "blog", "--name", "Добавить поле"], project=tmp_path
    )

    assert result.exit_code == 0, result.output
    assert (project.root / "scope_blog" / "migrations" / "0003_добавить_поле.py").exists()
    importlib.invalidate_caches()
    migrate_result = await CliCommands.run(["-c", project.config_argument, "migrate"], project=tmp_path)
    assert migrate_result.exit_code == 0, migrate_result.output
    assert "blog.0003_добавить_поле" in migrate_result.output


@pytest.mark.asyncio
async def test_migrate_dry_run_does_not_claim_migrations_were_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _build_blog_shop_project(tmp_path, monkeypatch, shop_depends_on_latest=True)
    assert (
        await CliCommands.run(["-c", project.config_argument, "migrate", "blog", "zero"], project=tmp_path)
    ).exit_code == 0

    result = await CliCommands.run(["-c", project.config_argument, "migrate", "--dry-run"], project=tmp_path)

    assert result.exit_code == 0, result.output
    assert "Applying" not in result.output
    assert "Would apply blog.0001_initial" in result.output
    assert "author" not in _table_names(project.database_path)


@pytest.mark.asyncio
async def test_migrate_zero_dry_run_does_not_claim_migrations_were_rolled_back(blog_shop_project, tmp_path) -> None:
    project = blog_shop_project
    result = await CliCommands.run(
        ["-c", project.config_argument, "migrate", "blog", "zero", "--dry-run"], project=tmp_path
    )

    assert result.exit_code == 0, result.output
    assert "Rolling back" not in result.output
    assert "Would roll back shop.0001_initial" in result.output
    assert "product" in _table_names(project.database_path)


@pytest.mark.asyncio
async def test_migrate_failure_ends_the_open_progress_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing step left `Rolling back X...` unterminated, gluing the error text onto it."""
    project = await _build_blog_shop_project(tmp_path, monkeypatch, shop_depends_on_latest=True)
    connection = sqlite3.connect(project.database_path)
    connection.execute("DROP TABLE product")
    connection.execute("CREATE VIEW product AS SELECT 1 AS id")
    connection.commit()
    connection.close()

    result = await CliCommands.run(["-c", project.config_argument, "migrate", "shop", "zero"], project=tmp_path)

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert result.stdout.endswith("FAILED\n")
    assert "Rolling back shop.0001_initial..." in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        ["migrate", "ghost"],
        # An empty app label migrated every app.
        ["migrate", ""],
        ["migrate", "ghost", "0001_initial"],
        ["migrate", "ghost.0001_initial"],
        ["migrate", "ghost", "zero"],
        ["sqlmigrate", "ghost", "0001_initial"],
        ["history", "ghost"],
        ["heads", "ghost"],
        ["makemigrations", "ghost"],
        ["squashmigrations", "ghost", "0001_initial"],
        ["drift", "ghost"],
        ["init", "ghost"],
    ],
)
async def test_unknown_app_label_exits_with_usage_error_code(
    blog_shop_project, arguments: list[str], tmp_path
) -> None:
    project = blog_shop_project
    result = await CliCommands.run(["-c", project.config_argument, *arguments], project=tmp_path)

    assert result.exit_code == 2, result.output
    assert "Unknown app label" in result.output
    assert "Traceback" not in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # An empty migration migrated every one, an empty connection was the default one.
        ["migrate", "blog", ""],
        ["sqlmigrate", "blog", " "],
        ["squashmigrations", "blog", ""],
        ["makemigrations", "blog", "--name", ""],
        ["drift", "--connection", ""],
        ["inspectdb", "--connection", ""],
        ["inspectdb", "--schema", ""],
    ],
)
async def test_an_empty_name_exits_with_usage_error_code(blog_shop_project, arguments: list[str], tmp_path) -> None:
    result = await CliCommands.run(["-c", blog_shop_project.config_argument, *arguments], project=tmp_path)

    assert result.exit_code == 2, result.output
    assert "takes a name" in result.output
    assert "Traceback" not in result.output


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("older_than", ["-1", "-99999999999999999999", "abc", "1.5"])
async def test_distributed_recover_rejects_out_of_range_older_than(older_than: str, tmp_path) -> None:
    result = await CliCommands.run(
        ["-c", "unused.HARE_ORM", "distributed-recover", "--coordinator", "coordinator", "--older-than", older_than],
        project=tmp_path,
    )

    assert result.exit_code == 2
    assert "Traceback" not in result.output


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_distributed_recover_older_than_error_names_the_allowed_range(tmp_path) -> None:
    result = await CliCommands.run(
        ["-c", "unused.HARE_ORM", "distributed-recover", "--coordinator", "coordinator", "--older-than", "-5"],
        project=tmp_path,
    )

    assert result.exit_code == 2
    assert "--older-than" in result.output
    assert "at least 0" in result.output


@pytest.mark.database_independent
@pytest.mark.parametrize("older_than", ["0", "315360001", "99999999999999999999"])
def test_distributed_recover_older_than_accepts_nonnegative_integers(older_than: str) -> None:
    assert ArgParserBuilder.parse_older_than_seconds(older_than) == int(older_than)


@pytest.mark.asyncio
async def test_makemigrations_name_rewrites_cross_app_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`makemigrations -n start` renamed blog's migration to 0001_start after shop's writer had
    already recorded a dependency on ('blog', '0001_initial'), which then never existed."""
    ProjectSources(
        tmp_path,
    ).write_package("scope_blog", BLOG_MODELS.format(extra=""))
    ProjectSources(
        tmp_path,
    ).write_package("scope_shop", SHOP_MODELS)
    database_path = tmp_path / "scope.sqlite3"
    settings_name = f"scope_settings_{tmp_path.name}"
    (tmp_path / f"{settings_name}.py").write_text(
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite://{database_path.as_posix()}"}},
    "apps": {{
        "blog": {{"models": ["scope_blog.models"], "default_connection": "default"}},
        "shop": {{"models": ["scope_shop.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    config_argument = f"{settings_name}.HARE_ORM"
    result = await CliCommands.run(["-c", config_argument, "makemigrations", "-n", "start"], project=tmp_path)

    assert result.exit_code == 0, result.output
    migration_class = importlib.import_module("scope_shop.migrations.0001_start").Migration
    assert migration_class.dependencies == [("blog", "0001_start")]
    importlib.invalidate_caches()
    migrate_result = await CliCommands.run(["-c", config_argument, "migrate"], project=tmp_path)
    assert migrate_result.exit_code == 0, migrate_result.output
    assert {"author", "product"} <= _table_names(database_path)


def _run_cli_subprocess(project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    hare_source_root = Path(cli_module.__file__).resolve().parents[2]
    python_path = os.pathsep.join(
        part for part in (str(project_root), str(hare_source_root), os.environ.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "hare.cli", "-c", "flat_settings.HARE_ORM", *arguments],
        cwd=project_root,
        env={**os.environ, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        check=False,
    )


FLAT_MODELS = """
from hare import fields
from hare.models import Model


class Thing(Model):
    id = fields.IntField(primary_key=True)
"""


@pytest.mark.database_independent
def test_flat_models_module_gets_top_level_migrations_package(tmp_path: Path) -> None:
    """A top-level models.py (no package) used to infer `models.migrations`, crash makemigrations
    with a ModuleNotFoundError traceback and make migrate/heads silently report nothing."""
    (tmp_path / "models.py").write_text(FLAT_MODELS, encoding="utf-8")
    database_path = tmp_path / "flat.sqlite3"
    (tmp_path / "flat_settings.py").write_text(
        f'HARE_ORM = {{"connections": {{"default": "sqlite+aiosqlite://{database_path.as_posix()}"}}, '
        '"apps": {"models": {"models": ["models"]}}}\n',
        encoding="utf-8",
    )

    makemigrations_result = _run_cli_subprocess(tmp_path, "makemigrations")
    assert makemigrations_result.returncode == 0, makemigrations_result.stdout + makemigrations_result.stderr
    assert (tmp_path / "migrations" / "0001_initial.py").exists()

    migrate_result = _run_cli_subprocess(tmp_path, "migrate")
    assert migrate_result.returncode == 0, migrate_result.stdout + migrate_result.stderr
    assert "models.0001_initial" in migrate_result.stdout
    assert "thing" in _table_names(database_path)


@pytest.mark.database_independent
def test_unimportable_migrations_module_is_a_clean_cli_error(tmp_path: Path) -> None:
    (tmp_path / "models.py").write_text(FLAT_MODELS, encoding="utf-8")
    (tmp_path / "flat_settings.py").write_text(
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["models"], "migrations": "models.migrations"}}}\n',
        encoding="utf-8",
    )

    result = _run_cli_subprocess(tmp_path, "heads")

    assert result.returncode != 0
    assert "Traceback" not in result.stdout + result.stderr
    assert "models.migrations" in result.stdout + result.stderr
