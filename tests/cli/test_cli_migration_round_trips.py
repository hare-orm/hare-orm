"""Full CLI migration cycles against a real database (SQLite file, or a fresh Postgres database
when HARE_TEST_DB points at Postgres): makemigrations -> migrate with data -> makemigrations
--check -> drift -> rollback, each command run the way a user runs it, re-reading the written
migration files every time."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import os
import sys
import textwrap
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from hare.cli import hare_cli as cli_module
from hare.core.connections import Connections
from hare.core.context import HareContext
from hare.exceptions import OperationalError
from tests.utils.database_under_test import DatabaseUnderTest

MODELS_HEADER = (
    "from hare import fields\nfrom hare.ddl.constraints import UniqueConstraint\nfrom hare.models import Model\n"
)


class CliProject:
    """A throwaway project on disk (settings + one package per app) driven through the CLI."""

    def __init__(self, root: Path, database_url: str, app_labels: tuple[str, ...], settings_module: str) -> None:
        self.root = root
        self.database_url = database_url
        self.app_labels = app_labels
        self.settings_module = settings_module
        self.is_postgres = DatabaseUnderTest.get_dialect(database_url).name == "postgresql"
        apps_config = {
            app_label: {
                "models": [f"cli_rt_{app_label}.models"],
                "default_connection": "default",
                "migrations": f"cli_rt_{app_label}.migrations",
            }
            for app_label in app_labels
        }
        self.config = {"connections": {"default": database_url}, "apps": apps_config}
        (root / f"{settings_module}.py").write_text(f"HARE_ORM = {self.config!r}\n", encoding="utf-8")
        for app_label in app_labels:
            migrations_package = root / f"cli_rt_{app_label}" / "migrations"
            migrations_package.mkdir(parents=True)
            (root / f"cli_rt_{app_label}" / "__init__.py").write_text("", encoding="utf-8")
            (migrations_package / "__init__.py").write_text("", encoding="utf-8")
            self.write_models("", app_label)

    def write_models(self, source: str, app_label: str = "app") -> None:
        (self.root / f"cli_rt_{app_label}" / "models.py").write_text(
            MODELS_HEADER + textwrap.dedent(source), encoding="utf-8"
        )

    def write_migration(self, name: str, source: str, app_label: str = "app") -> None:
        (self.root / f"cli_rt_{app_label}" / "migrations" / f"{name}.py").write_text(
            textwrap.dedent(source), encoding="utf-8"
        )

    def migration_files(self, app_label: str = "app") -> list[str]:
        return sorted(path.stem for path in (self.root / f"cli_rt_{app_label}" / "migrations").glob("0*.py"))

    def read_latest_migration(self, app_label: str = "app") -> str:
        latest_name = self.migration_files(app_label)[-1]
        return (self.root / f"cli_rt_{app_label}" / "migrations" / f"{latest_name}.py").read_text(encoding="utf-8")

    async def cli(self, *args: str) -> SimpleNamespace:
        for module_name in [name for name in sys.modules if name.startswith("cli_rt_")]:
            del sys.modules[module_name]
        importlib.invalidate_caches()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = await cli_module.HareCLI.run_cli_async(["-c", f"{self.settings_module}.HARE_ORM", *args])
        return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())

    async def cli_ok(self, *args: str) -> str:
        result = await self.cli(*args)
        assert result.exit_code in (0, None), result.output
        return result.output

    async def query(self, sql: str) -> list[dict[str, Any]]:
        async with HareContext() as context:
            await context.init(config=get_probe_config(self.database_url))
            try:
                return await Connections.get("default").execute_dicts(sql)
            finally:
                await context.connections.close_all(discard=False)

    async def column_types(self, table: str) -> dict[str, str]:
        if self.is_postgres:
            rows = await self.query(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND table_schema = current_schema()"
            )
            return {row["column_name"]: row["data_type"] for row in rows}
        rows = await self.query(f"PRAGMA table_info('{table}')")
        return {row["name"]: row["type"].lower() for row in rows}

    async def assert_in_sync(self) -> None:
        check_result = await self.cli("makemigrations", "--check")
        assert check_result.exit_code in (0, None), check_result.output
        assert "No changes detected" in check_result.output
        drift_result = await self.cli("drift", *self.app_labels)
        assert drift_result.exit_code in (0, None), drift_result.output


def get_probe_config(database_url: str) -> dict[str, Any]:
    """A config opening `database_url` without importing any project app's models."""
    return {"connections": {"default": database_url}, "apps": {"probe": {"models": ["cli_rt_probe"]}}}


def get_postgres_database_template() -> str | None:
    raw_database_url = os.environ.get("HARE_TEST_DB", "sqlite://:memory:")
    if DatabaseUnderTest.is_file_database(raw_database_url):
        return None
    return raw_database_url.replace("\\{", "{").replace("\\}", "}")


@pytest_asyncio.fixture
async def project_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any]:
    """Builds CliProject instances on a fresh database each; drops Postgres databases afterwards."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    (tmp_path / "cli_rt_probe.py").write_text("", encoding="utf-8")
    postgres_template = get_postgres_database_template()
    created_projects: list[CliProject] = []

    async def create_project(*app_labels: str) -> CliProject:
        project_index = len(created_projects)
        project_root = tmp_path / f"project_{project_index}"
        project_root.mkdir()
        monkeypatch.syspath_prepend(str(project_root))
        if postgres_template is None:
            scheme = DatabaseUnderTest.get_url().split("://", 1)[0]
            database_url = f"{scheme}://{(project_root / 'db.sqlite3').as_posix()}"
        else:
            database_name = f"hare_cli_rt_{uuid.uuid4().hex}"
            database_url = (
                postgres_template.format(database_name)
                if "{}" in postgres_template
                else f"{postgres_template}_{database_name}"
            )
            async with HareContext() as context:
                await context.init(config=get_probe_config(database_url), _create_db=True)
                await context.connections.close_all(discard=False)
        project = CliProject(
            project_root, database_url, app_labels or ("app",), f"cli_rt_settings_{tmp_path.name}_{project_index}"
        )
        created_projects.append(project)
        return project

    yield create_project

    for project in created_projects:
        if not project.is_postgres:
            continue
        async with HareContext() as context:
            await context.init(config=get_probe_config(project.database_url))
            connection = Connections.get("default")
            await context.connections.close_all(discard=False)
            for attempt in range(20):
                try:
                    await connection.db_delete()
                    break
                except OperationalError:
                    if attempt == 19:
                        raise
                    await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_primary_key_type_change_is_carried_to_referencing_columns(project_factory) -> None:
    project = await project_factory()
    template = """
    class Author(Model):
        id = fields.{pk_type}(primary_key=True)
        code = fields.CharField(max_length={code_length}, unique=True)
    class Book(Model):
        id = fields.{pk_type}(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books")
        editor = fields.ForeignKeyField("app.Author", related_name="edited_books", to_field="code", null=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books")
    class Tag(Model):
        id = fields.{pk_type}(primary_key=True)
    """
    project.write_models(template.format(pk_type="IntField", code_length=10))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id, code) VALUES (1, 'a')")
    await project.query("INSERT INTO tag (id) VALUES (1)")
    await project.query("INSERT INTO book (id, author_id, editor_id) VALUES (1, 1, 'a')")
    await project.query("INSERT INTO book_tag (book_id, tag_id) VALUES (1, 1)")

    project.write_models(template.format(pk_type="BigIntField", code_length=40))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    big_value = 3_000_000_000
    await project.query(f"INSERT INTO author (id, code) VALUES ({big_value}, '{'x' * 40}')")
    await project.query(f"INSERT INTO tag (id) VALUES ({big_value})")
    await project.query(f"INSERT INTO book (id, author_id, editor_id) VALUES ({big_value}, {big_value}, '{'x' * 40}')")
    await project.query(f"INSERT INTO book_tag (book_id, tag_id) VALUES ({big_value}, {big_value})")
    book_types = await project.column_types("book")
    through_types = await project.column_types("book_tag")
    if project.is_postgres:
        assert book_types["author_id"] == "bigint"
        assert book_types["editor_id"] == "character varying"
        assert through_types == {"book_id": "bigint", "tag_id": "bigint"}
    else:
        assert book_types["author_id"] == "bigint"
        assert book_types["editor_id"] == "varchar(40)"
        assert through_types == {"book_id": "bigint", "tag_id": "bigint"}

    await project.query(f"DELETE FROM book_tag WHERE book_id = {big_value}")
    await project.query(f"DELETE FROM book WHERE id = {big_value}")
    await project.query(f"DELETE FROM author WHERE id = {big_value}")
    await project.query(f"DELETE FROM tag WHERE id = {big_value}")
    await project.cli_ok("migrate", "app", project.migration_files()[0])

    assert (await project.column_types("book"))["author_id"] in ("integer", "int")
    assert (await project.column_types("book_tag"))["tag_id"] in ("integer", "int")
    assert await project.query("SELECT id, author_id, editor_id FROM book") == [
        {"id": 1, "author_id": 1, "editor_id": "a"}
    ]
    assert await project.query("SELECT book_id, tag_id FROM book_tag") == [{"book_id": 1, "tag_id": 1}]


@pytest.mark.asyncio
async def test_added_and_altered_relations_produce_no_follow_up_changes(project_factory) -> None:
    project = await project_factory()
    base = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
    """
    project.write_models(base)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1), (2)")
    await project.query("INSERT INTO book (id) VALUES (1), (2)")

    relation_versions = [
        'author = fields.ForeignKeyField("app.Author", related_name="books", null=True, on_delete=fields.SET_NULL)',
        'author = fields.ForeignKeyField("app.Author", related_name="books", null=True, '
        "on_delete=fields.SET_NULL, db_constraint=False)",
        'author = fields.OneToOneField("app.Author", related_name="book", null=True, on_delete=fields.SET_NULL)',
        'author = fields.ForeignKeyField("app.Author", related_name="books", default=1)',
    ]
    for relation_declaration in relation_versions:
        project.write_models(base + f"    {relation_declaration}\n")
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        if "default=1" not in relation_declaration:
            await project.query("UPDATE book SET author_id = id")

    assert await project.query("SELECT id, author_id FROM book ORDER BY id") == [
        {"id": 1, "author_id": 1},
        {"id": 2, "author_id": 2},
    ]
    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert "author_id" not in await project.column_types("book")


@pytest.mark.asyncio
async def test_initial_many_to_many_through_model_produces_no_follow_up_changes(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")
    class BookTag(Model):
        id = fields.IntField(primary_key=True)
        book = fields.ForeignKeyField("app.Book", related_name="book_tags")
        tag = fields.ForeignKeyField("app.Tag", related_name="book_tags")
        class Meta:
            table = "book_tag_link"
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_rename_field_rewrites_generated_expression_in_state(project_factory) -> None:
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        {column} = fields.IntField(db_default=5)
        qty = fields.IntField()
        total = fields.GeneratedField(expression="{column} * qty", output_field=fields.IntField())
    """
    project.write_models(template.format(column="price"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, price, qty) VALUES (1, 3, 4)")

    project.write_models(template.format(column="amount"))
    project.write_migration(
        "0002_rename_price",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Item", old_name="price", new_name="amount")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO item (id, qty) VALUES (2, 10)")
    assert await project.query("SELECT id, amount, total FROM item ORDER BY id") == [
        {"id": 1, "amount": 3, "total": 12},
        {"id": 2, "amount": 5, "total": 50},
    ]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, price, total FROM item ORDER BY id") == [
        {"id": 1, "price": 3, "total": 12},
        {"id": 2, "price": 5, "total": 50},
    ]


@pytest.mark.asyncio
async def test_changed_generated_field_is_recreated_after_the_field_it_now_uses(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(default=1)
        qty = fields.IntField()
        total = fields.GeneratedField(expression="price * qty", output_field=fields.IntField())
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, price, qty) VALUES (1, 3, 4)")

    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(default=1)
        qty = fields.IntField()
        discount = fields.BigIntField(default=2)
        total = fields.GeneratedField(expression="price * qty - discount", output_field=fields.IntField())
    """)
    await project.cli_ok("makemigrations")
    migration_source = project.read_latest_migration()
    assert migration_source.index('name="discount"') < migration_source.index('name="total",')
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, discount, total FROM item") == [{"id": 1, "discount": 2, "total": 10}]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, total FROM item") == [{"id": 1, "total": 12}]


@pytest.mark.asyncio
@pytest.mark.parametrize("through_table", ["book_tag", "book_tag_link"])
async def test_automatic_through_table_switched_to_through_model_keeps_rows(
    project_factory, through_table: str
) -> None:
    project = await project_factory()
    automatic = """
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books")
    """
    with_through_model = f"""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")
    class BookTag(Model):
        id = fields.IntField(primary_key=True)
        book = fields.ForeignKeyField("app.Book", related_name="book_tags")
        tag = fields.ForeignKeyField("app.Tag", related_name="book_tags")
        class Meta:
            table = "{through_table}"
            constraints = (UniqueConstraint(fields=("book", "tag")),)
    """
    project.write_models(automatic)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id) VALUES (1), (2)")
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO book_tag (book_id, tag_id) VALUES (1, 1), (1, 2)")

    project.write_models(with_through_model)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query(f"SELECT book_id, tag_id FROM {through_table} ORDER BY tag_id") == [
        {"book_id": 1, "tag_id": 1},
        {"book_id": 1, "tag_id": 2},
    ]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT book_id, tag_id FROM book_tag ORDER BY tag_id") == [
        {"book_id": 1, "tag_id": 1},
        {"book_id": 1, "tag_id": 2},
    ]


@pytest.mark.asyncio
async def test_through_model_switched_back_to_automatic_table_of_the_same_name_keeps_rows(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")
    class BookTag(Model):
        id = fields.IntField(primary_key=True)
        book = fields.ForeignKeyField("app.Book", related_name="book_tags")
        tag = fields.ForeignKeyField("app.Tag", related_name="book_tags")
        class Meta:
            table = "book_tag"
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id) VALUES (1), (2)")
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO book_tag (id, book_id, tag_id) VALUES (1, 1, 1), (2, 1, 2), (3, 1, 2)")

    project.write_models("""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books")
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT book_id, tag_id FROM book_tag ORDER BY tag_id") == [
        {"book_id": 1, "tag_id": 1},
        {"book_id": 1, "tag_id": 2},
    ]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT book_id, tag_id FROM book_tag ORDER BY tag_id") == [
        {"book_id": 1, "tag_id": 1},
        {"book_id": 1, "tag_id": 2},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("table_option", ['\n    class Meta:\n        table = "tag"', ""])
async def test_model_moved_between_apps_keeps_its_table(project_factory, table_option: str) -> None:
    project = await project_factory("a", "b")
    tag_source = (
        "class Tag(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        f"    name = fields.CharField(max_length=20){table_option}\n"
    )
    project.write_models(
        tag_source + "class Post(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    tag = fields.ForeignKeyField("a.Tag", related_name="posts")\n',
        "a",
    )
    project.write_models("class Other(Model):\n    id = fields.IntField(primary_key=True)\n", "b")
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id, name) VALUES (1, 'x')")
    await project.query("INSERT INTO post (id, tag_id) VALUES (1, 1)")

    project.write_models(
        "class Post(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    tag = fields.ForeignKeyField("b.Tag", related_name="posts")\n',
        "a",
    )
    project.write_models("class Other(Model):\n    id = fields.IntField(primary_key=True)\n" + tag_source, "b")
    await project.cli_ok("makemigrations")
    assert "state_only=True" in project.read_latest_migration("a")
    assert "state_only=True" in project.read_latest_migration("b")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, name FROM tag") == [{"id": 1, "name": "x"}]
    assert await project.query("SELECT id, tag_id FROM post") == [{"id": 1, "tag_id": 1}]

    await project.cli_ok("migrate", "a", "0001_initial")
    await project.cli_ok("migrate", "b", "0001_initial")
    assert await project.query("SELECT id, name FROM tag") == [{"id": 1, "name": "x"}]
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_model_moved_between_apps_with_changes_is_refused_without_writing_migrations(project_factory) -> None:
    project = await project_factory("a", "b")
    project.write_models("class Tag(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models("class Other(Model):\n    id = fields.IntField(primary_key=True)\n", "b")
    await project.cli_ok("makemigrations")

    project.write_models("", "a")
    project.write_models(
        "class Other(Model):\n    id = fields.IntField(primary_key=True)\n"
        "class Tag(Model):\n    id = fields.IntField(primary_key=True)\n"
        "    name = fields.CharField(max_length=20, default='')\n",
        "b",
    )
    result = await project.cli("makemigrations")

    assert result.exit_code == 1
    assert "is moved to b.Tag" in result.output
    assert "Traceback" not in result.output
    assert project.migration_files("a") == ["0001_initial"]
    assert project.migration_files("b") == ["0001_initial"]


@pytest.mark.asyncio
async def test_deleting_a_model_referenced_from_another_app_waits_for_that_app(project_factory) -> None:
    project = await project_factory("a", "b")
    project.write_models(
        "class Author(Model):\n    id = fields.IntField(primary_key=True)\n"
        "class Keep(Model):\n    id = fields.IntField(primary_key=True)\n",
        "a",
    )
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    author = fields.ForeignKeyField("a.Author", related_name="books", null=True)\n'
        '    coauthors = fields.ManyToManyField("a.Author", related_name="cobooks")\n',
        "b",
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1)")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 1)")

    project.write_models("class Keep(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models("class Book(Model):\n    id = fields.IntField(primary_key=True)\n", "b")
    only_a_result = await project.cli("makemigrations", "a")
    assert only_a_result.exit_code == 1
    assert "still references it" in only_a_result.output
    assert project.migration_files("a") == ["0001_initial"]

    await project.cli_ok("makemigrations")
    assert '("b", ' in project.read_latest_migration("a").split("dependencies = ", 1)[1].split("\n", 1)[0]
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id FROM book") == [{"id": 1}]

    await project.cli_ok("migrate", "a", "0001_initial")
    await project.cli_ok("migrate", "b", "0001_initial")
    assert await project.query("SELECT id FROM author") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delete_dependencies", [[("a", "0001_initial"), ("b", "0001_initial")], [("a", "0001_initial")]]
)
async def test_migration_files_deleting_a_still_referenced_model_report_a_clean_error(
    project_factory, delete_dependencies: list[tuple[str, str]]
) -> None:
    project = await project_factory("a", "b")
    project.write_models("class Author(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    author = fields.ForeignKeyField("a.Author", related_name="books")\n',
        "b",
    )
    await project.cli_ok("makemigrations")
    project.write_migration(
        "0002_delete_author",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = DELETE_DEPENDENCIES
            operations = [ops.DeleteModel(name="Author")]
        """.replace("DELETE_DEPENDENCIES", repr(delete_dependencies)),
        "a",
    )
    project.write_migration(
        "0002_remove_author",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("b", "0001_initial")]
            operations = [ops.RemoveField(model_name="Book", name="author")]
        """,
        "b",
    )
    project.write_models("", "a")
    project.write_models("class Book(Model):\n    id = fields.IntField(primary_key=True)\n", "b")

    result = await project.cli("makemigrations", "--check")

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "b.Book.author -> a.Author" in result.output or "still referenced from b.Book" in result.output


@pytest.mark.asyncio
async def test_to_field_change_refuses_rows_and_repoints_an_empty_relation(project_factory) -> None:
    project = await project_factory()
    template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        code = fields.CharField(max_length=10, unique=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books", null=True{extra})
    """
    project.write_models(template.format(extra=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id, code) VALUES (1, 'a'), (2, 'b')")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 2), (2, NULL)")

    project.write_models(template.format(extra=', to_field="code"'))
    makemigrations_output = await project.cli_ok("makemigrations")
    assert "to_field" in makemigrations_output
    refused = await project.cli("migrate")
    assert refused.exit_code == 1
    assert "Cannot repoint Book.author" in refused.output
    assert await project.query("SELECT id, author_id FROM book ORDER BY id") == [
        {"id": 1, "author_id": 2},
        {"id": 2, "author_id": None},
    ]

    await project.query("UPDATE book SET author_id = NULL")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("UPDATE book SET author_id = 'b' WHERE id = 1")
    if DatabaseUnderTest.get_dialect().supports_foreign_keys:
        with pytest.raises(Exception, match=r"(?i)foreign key"):
            await project.query("UPDATE book SET author_id = 'missing' WHERE id = 2")

    await project.query("UPDATE book SET author_id = NULL")
    await project.cli_ok("migrate", "app", "0001_initial")
    await project.query("UPDATE book SET author_id = 1 WHERE id = 1")
    assert await project.query("SELECT id, author_id FROM book ORDER BY id") == [
        {"id": 1, "author_id": 1},
        {"id": 2, "author_id": None},
    ]


@pytest.mark.asyncio
async def test_deleting_a_model_next_to_an_unchanged_relation_into_the_same_app(project_factory) -> None:
    project = await project_factory("a", "b")
    project.write_models(
        "class Author(Model):\n    id = fields.IntField(primary_key=True)\n"
        "class Keep(Model):\n    id = fields.IntField(primary_key=True)\n",
        "a",
    )
    book_with_keep = (
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    keep = fields.ForeignKeyField("a.Keep", related_name="books", null=True)\n'
    )
    project.write_models(
        book_with_keep + '    author = fields.ForeignKeyField("a.Author", related_name="books", null=True)\n', "b"
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO keep (id) VALUES (1)")
    await project.query("INSERT INTO book (id, keep_id) VALUES (1, 1)")

    project.write_models(
        "class Keep(Model):\n    id = fields.IntField(primary_key=True)\n"
        "    label = fields.CharField(max_length=10, default='')\n",
        "a",
    )
    project.write_models(book_with_keep, "b")
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, keep_id FROM book") == [{"id": 1, "keep_id": 1}]


@pytest.mark.asyncio
async def test_new_migrations_depending_on_each_other_in_a_cycle_are_refused(project_factory) -> None:
    project = await project_factory("a", "b")
    project.write_models(
        "class Author(Model):\n    id = fields.IntField(primary_key=True)\n"
        "class Keep(Model):\n    id = fields.IntField(primary_key=True)\n",
        "a",
    )
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    author = fields.ForeignKeyField("a.Author", related_name="books", null=True)\n',
        "b",
    )
    await project.cli_ok("makemigrations")

    project.write_models("class Keep(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    keep = fields.ForeignKeyField("a.Keep", related_name="books", null=True)\n',
        "b",
    )
    result = await project.cli("makemigrations")

    assert result.exit_code == 1
    assert "cycle" in result.output
    assert "Traceback" not in result.output
    assert project.migration_files("a") == ["0001_initial"]
    assert project.migration_files("b") == ["0001_initial"]


async def get_plain_index_column_lists(project: CliProject, table: str) -> list[tuple[str, ...]]:
    """The column lists of every plain (non-unique) index on `table`."""
    if project.is_postgres:
        rows = await project.query(
            "SELECT index_class.relname AS name, attribute.attname AS column_name, keys.position "
            "FROM pg_index index_info "
            "JOIN pg_class index_class ON index_class.oid = index_info.indexrelid "
            "JOIN pg_class table_class ON table_class.oid = index_info.indrelid "
            "CROSS JOIN LATERAL unnest(index_info.indkey) WITH ORDINALITY AS keys(attribute_number, position) "
            "JOIN pg_attribute attribute ON attribute.attrelid = table_class.oid "
            "AND attribute.attnum = keys.attribute_number "
            f"WHERE table_class.relname = '{table}' AND NOT index_info.indisunique AND NOT index_info.indisprimary"
        )
        columns_by_index_name: dict[str, list[tuple[int, str]]] = {}
        for row in rows:
            columns_by_index_name.setdefault(row["name"], []).append((row["position"], row["column_name"]))
        return sorted(
            tuple(column for _position, column in sorted(columns)) for columns in columns_by_index_name.values()
        )
    index_rows = await project.query(f"SELECT name FROM pragma_index_list('{table}') WHERE \"unique\" = 0")
    column_lists = []
    for index_row in index_rows:
        column_rows = await project.query(f"SELECT name FROM pragma_index_info('{index_row['name']}') ORDER BY seqno")
        column_lists.append(tuple(row["name"] for row in column_rows))
    return sorted(column_lists)


@pytest.mark.asyncio
async def test_foreign_key_indexes_are_added_to_relations_migrated_without_them(project_factory) -> None:
    project = await project_factory()
    template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
    class Edition(Model):
        number = fields.IntField()
        year = fields.IntField()
        pk = fields.CompositePrimaryKey("number", "year")
    class Book(Model):
        id = fields.IntField(primary_key=True)
        code = fields.IntField()
        author = fields.ForeignKeyField("app.Author", related_name="books"{index})
        reviewer = fields.ForeignKeyField("app.Author", related_name="reviews", db_index=False)
        owner = fields.ForeignKeyField("app.Author", related_name="owned_books"{index})
        edition = fields.ForeignKeyField("app.Edition", related_name="books"{index})
        tags = fields.ManyToManyField("app.Author", related_name="tagged_books"{index})

        class Meta:
            constraints = (UniqueConstraint(fields=("owner", "code")),)
    """
    # db_index=False writes the relations exactly the way a migration file predating the
    # default index declares them.
    project.write_models(template.format(index=", db_index=False"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    assert await get_plain_index_column_lists(project, "book") == []

    project.write_models(template.format(index=""))
    await project.cli_ok("makemigrations")
    migration_source = project.read_latest_migration()
    await project.cli_ok("migrate")

    assert migration_source.count("ops.AlterField(") == 3
    assert 'name="reviewer"' not in migration_source
    assert 'name="owner"' not in migration_source
    assert await get_plain_index_column_lists(project, "book") == [
        ("author_id",),
        ("edition_number", "edition_year"),
    ]
    assert await get_plain_index_column_lists(project, "book_author") == [("author_id",)]
    await project.assert_in_sync()

    await project.cli_ok("migrate", "app", project.migration_files()[0])

    assert await get_plain_index_column_lists(project, "book") == []
    assert await get_plain_index_column_lists(project, "book_author") == []


RENAMED_MODEL_RELATION_CASES = {
    "foreign_key": ('tag = fields.ForeignKeyField("app.Tag", related_name="authors")', ""),
    "many_to_many": ('tags = fields.ManyToManyField("app.Tag", related_name="authors")', ""),
    "many_to_many_explicit_table": (
        'tags = fields.ManyToManyField("app.Tag", related_name="authors")',
        '\n        class Meta:\n            table = "writers"',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(RENAMED_MODEL_RELATION_CASES))
async def test_renamed_model_with_a_related_name_relation_applies_and_rolls_back(project_factory, case: str) -> None:
    project = await project_factory()
    relation, meta = RENAMED_MODEL_RELATION_CASES[case]
    template = """
    class {name}(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
        {relation}{meta}
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    """
    project.write_models(template.format(name="Author", relation=relation, meta=meta))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    table = "writers" if meta else "author"
    await project.query("INSERT INTO tag (id) VALUES (1)")
    if "ForeignKeyField" in relation:
        await project.query(f"INSERT INTO {table} (id, name, tag_id) VALUES (1, 'a', 1)")
    else:
        await project.query(f"INSERT INTO {table} (id, name) VALUES (1, 'a')")

    project.write_models(template.format(name="Writer", relation=relation, meta=meta))
    await project.cli_ok("makemigrations")
    assert 'ops.RenameModel(old_name="Author", new_name="Writer")' in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    renamed_table = "writers" if meta else "writer"
    assert await project.query(f"SELECT id, name FROM {renamed_table}") == [{"id": 1, "name": "a"}]

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query(f"SELECT id, name FROM {table}") == [{"id": 1, "name": "a"}]


@pytest.mark.asyncio
async def test_switching_managed_off_and_on_never_touches_the_table(project_factory) -> None:
    project = await project_factory()
    managed = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
    class Box(Model):
        id = fields.IntField(primary_key=True)
        item = fields.ForeignKeyField("app.Item", related_name="boxes")
    """
    unmanaged = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
        note = fields.CharField(max_length=10, null=True)
        class Meta:
            managed = False
    class Box(Model):
        id = fields.IntField(primary_key=True)
        item = fields.ForeignKeyField("app.Item", related_name="boxes")
    """
    project.write_models(managed)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, name) VALUES (1, 'keep me')")
    await project.query("INSERT INTO box (id, item_id) VALUES (1, 1)")

    project.write_models(unmanaged)
    await project.cli_ok("makemigrations")
    unmanaged_migration = project.read_latest_migration()
    await project.cli_ok("migrate")
    assert "ops.AlterModelOptions(" in unmanaged_migration
    assert '"managed": False' in unmanaged_migration
    assert "DeleteModel" not in unmanaged_migration
    assert "AddField" not in unmanaged_migration
    await project.assert_in_sync()
    assert await project.query("SELECT id, name FROM item") == [{"id": 1, "name": "keep me"}]

    project.write_models(managed)
    await project.cli_ok("makemigrations")
    managed_migration = project.read_latest_migration()
    await project.cli_ok("migrate")
    assert "ops.AlterModelOptions(" in managed_migration
    assert "CreateModel" not in managed_migration
    await project.assert_in_sync()
    assert await project.query("SELECT id, name FROM item") == [{"id": 1, "name": "keep me"}]

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT id, name FROM item") == [{"id": 1, "name": "keep me"}]

    # A model the migrations stopped managing is dropped from them without dropping its table.
    await project.cli_ok("migrate")
    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
        class Meta:
            managed = False
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models("""
    class Other(Model):
        id = fields.IntField(primary_key=True)
    """)
    await project.cli_ok("makemigrations")
    assert 'ops.DeleteModel(name="Item")' in project.read_latest_migration()
    await project.cli_ok("migrate")
    assert await project.query("SELECT id, name FROM item") == [{"id": 1, "name": "keep me"}]


async def get_index_names(project: CliProject, table: str) -> list[str]:
    if project.is_postgres:
        rows = await project.query(
            f"SELECT indexname AS name FROM pg_indexes WHERE tablename = '{table}' AND schemaname = current_schema()"
        )
    else:
        rows = await project.query(
            f"SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = '{table}' AND sql IS NOT NULL"
        )
    return sorted(row["name"] for row in rows if not row["name"].endswith("_pkey"))


@pytest.mark.asyncio
async def test_unnamed_expression_indexes_are_changed_and_removed(project_factory) -> None:
    project = await project_factory()
    header = "from hare.ddl.indexes import Index\nfrom hare.query.functions import Lower, Upper\n"
    steps = [
        """
        class Item(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            class Meta:
                indexes = [Index(Lower("name")), Index(Upper("name"), unique=True)]
        """,
        """
        class Item(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            class Meta:
                indexes = [Index(Upper("name")), Index(Lower("name"), unique=True)]
        """,
        """
        class Item(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            class Meta:
                table = "goods"
                indexes = [Index(Upper("name"))]
        """,
        """
        class Thing(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            class Meta:
                table = "goods"
        """,
    ]
    expected_index_counts = [2, 2, 1, 0]
    for source, expected_index_count in zip(steps, expected_index_counts, strict=True):
        project.write_models(header + textwrap.dedent(source))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        table = "goods" if "goods" in source else "item"
        assert len(await get_index_names(project, table)) == expected_index_count

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert len(await get_index_names(project, "item")) == 2


@pytest.mark.asyncio
async def test_model_replaced_by_one_reusing_its_related_name(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Author(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        pages = fields.IntField()
        author = fields.ForeignKeyField("app.Author", related_name="books")
    class Review(Model):
        id = fields.IntField(primary_key=True)
        book = fields.ForeignKeyField("app.Book", related_name="reviews", null=True)
    class Shelf(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="shelves")
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1)")

    project.write_models("""
    class Author(Model):
        id = fields.IntField(primary_key=True)
    class Novel(Model):
        id = fields.IntField(primary_key=True)
        title = fields.CharField(max_length=50)
        author = fields.ForeignKeyField("app.Author", related_name="books")
    class Review(Model):
        id = fields.IntField(primary_key=True)
        book = fields.ForeignKeyField("app.Novel", related_name="reviews", null=True)
    class Cabinet(Model):
        id = fields.IntField(primary_key=True)
        label = fields.CharField(max_length=10)
        author = fields.ForeignKeyField("app.Author", related_name="shelves")
    """)
    await project.cli_ok("makemigrations")
    migration_source = project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    # Review still references Book until its relation is repointed - only Book's conflicting
    # relation goes first; nothing references Shelf, so it's deleted before Cabinet is created.
    assert migration_source.index('ops.RemoveField(model_name="Book", name="author")') < migration_source.index(
        'name="Novel"'
    )
    assert migration_source.index('ops.DeleteModel(name="Shelf")') < migration_source.index('name="Cabinet"')
    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT id FROM author") == [{"id": 1}]


@pytest.mark.asyncio
async def test_identical_models_renamed_together_warn_about_an_unrecognized_rename(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Alpha(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
    class Beta(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
    """)
    await project.cli_ok("makemigrations")
    project.write_models("""
    class Gamma(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
    class Delta(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
    """)

    output = await project.cli_ok("makemigrations")

    assert "possible unrecognized rename(s)" in output
    assert "Model 'Gamma' (app 'app') has the exact same fields/options as removed model(s) 'Alpha', 'Beta'" in output
    assert "Model 'Delta' (app 'app')" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option",
    ['extensions = ("pg_trgm",)', 'table_description = "Items table"', 'soft_delete_field = "deleted_at"'],
)
async def test_removed_model_option_leaves_no_follow_up_changes(project_factory, option: str) -> None:
    project = await project_factory()
    if option.startswith("extensions") and not project.is_postgres:
        pytest.skip("Meta.extensions is Postgres-only")
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        deleted_at = fields.DatetimeField(null=True)
        class Meta:
            table = "item"
            {option}
    """
    project.write_models(template.format(option=option))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(template.format(option="pass"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    if project.is_postgres:
        assert await project.query("SELECT obj_description('item'::regclass, 'pg_class') AS comment") == [
            {"comment": None}
        ]

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    if project.is_postgres and option.startswith("table_description"):
        assert await project.query("SELECT obj_description('item'::regclass, 'pg_class') AS comment") == [
            {"comment": "Items table"}
        ]


@pytest.mark.asyncio
async def test_changed_table_description_updates_the_table_comment(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("only Postgres stores a table comment separately")
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        class Meta:
            table_description = {description!r}
    """
    project.write_models(template.format(description="Items table"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(template.format(description="Renamed's"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT obj_description('item'::regclass, 'pg_class') AS comment") == [
        {"comment": "Renamed's"}
    ]

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT obj_description('item'::regclass, 'pg_class') AS comment") == [
        {"comment": "Items table"}
    ]


@pytest.mark.asyncio
async def test_rows_moved_into_a_through_model_get_its_python_defaults(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books")
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id) VALUES (1), (2)")
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO book_tag (book_id, tag_id) VALUES (1, 1), (1, 2)")

    project.write_models("""
    def get_default_note():
        return "from-callable"
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")
    class BookTag(Model):
        id = fields.IntField(primary_key=True, generated=True)
        book = fields.ForeignKeyField("app.Book", related_name="book_tags")
        tag = fields.ForeignKeyField("app.Tag", related_name="tag_books")
        weight = fields.IntField(default=7)
        note = fields.CharField(max_length=20, default=get_default_note)
        created = fields.DatetimeField(auto_now_add=True)
        class Meta:
            table = "book_tag"
            constraints = (UniqueConstraint(fields=("book", "tag")),)
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    rows = await project.query("SELECT tag_id, weight, note, created FROM book_tag ORDER BY tag_id")
    assert [(row["tag_id"], row["weight"], row["note"]) for row in rows] == [
        (1, 7, "from-callable"),
        (2, 7, "from-callable"),
    ]
    assert all(row["created"] is not None for row in rows)

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT book_id, tag_id FROM book_tag ORDER BY tag_id") == [
        {"book_id": 1, "tag_id": 1},
        {"book_id": 1, "tag_id": 2},
    ]


@pytest.mark.asyncio
async def test_rows_moved_into_a_through_model_refuse_a_callable_default_of_a_unique_field(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books")
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models("""
    import uuid
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")
    class BookTag(Model):
        id = fields.IntField(primary_key=True, generated=True)
        book = fields.ForeignKeyField("app.Book", related_name="book_tags")
        tag = fields.ForeignKeyField("app.Tag", related_name="tag_books")
        token = fields.UUIDField(default=uuid.uuid4, unique=True)
        class Meta:
            table = "book_tag"
    """)
    await project.cli_ok("makemigrations")
    result = await project.cli("migrate")

    assert result.exit_code not in (0, None)
    assert "its unique field 'token' has a callable default" in result.output


@pytest.mark.asyncio
async def test_schema_change_moves_the_automatic_through_tables_along(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("Meta.schema is Postgres-only")
    database_name = (await project.query("SELECT current_database() AS name"))[0]["name"]
    # A current schema other than "public" - a table moved out of a schema goes back there.
    await project.query("CREATE SCHEMA app_space")
    await project.query(f'ALTER DATABASE "{database_name}" SET search_path TO app_space')
    template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField("app.Tag", related_name="authors"){meta}
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    """
    project.write_models(template.format(meta=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1)")
    await project.query("INSERT INTO tag (id) VALUES (1)")
    await project.query("INSERT INTO author_tag (author_id, tag_id) VALUES (1, 1)")

    async def get_table_schemas() -> dict[str, str]:
        rows = await project.query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_name IN ('author', 'author_tag', 'tag')"
        )
        return {row["table_name"]: row["table_schema"] for row in rows}

    project.write_models(template.format(meta='\n        class Meta:\n            schema = "lib"'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await get_table_schemas() == {"author": "lib", "author_tag": "lib", "tag": "app_space"}
    assert await project.query("SELECT author_id, tag_id FROM lib.author_tag") == [{"author_id": 1, "tag_id": 1}]

    project.write_models(template.format(meta=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await get_table_schemas() == {"author": "app_space", "author_tag": "app_space", "tag": "app_space"}

    await project.cli_ok("migrate", "app", project.migration_files()[1])
    assert await get_table_schemas() == {"author": "lib", "author_tag": "lib", "tag": "app_space"}
    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await get_table_schemas() == {"author": "app_space", "author_tag": "app_space", "tag": "app_space"}


@pytest.mark.asyncio
async def test_one_to_one_primary_key_is_renamed_and_moved_to_another_column(project_factory) -> None:
    project = await project_factory()
    template = """
    class Account(Model):
        id = fields.BigIntField(primary_key=True)
    class Profile(Model):
        {key_name} = fields.OneToOneField("app.Account", related_name="profile", primary_key=True{source_field})
        note = fields.CharField(max_length=10, default="n")
    class Visit(Model):
        id = fields.IntField(primary_key=True)
        profile = fields.ForeignKeyField("app.Profile", related_name="visits")
        extra = fields.OneToOneField("app.Profile", related_name="extra_visit", null=True)
    """
    project.write_models(template.format(key_name="owner", source_field=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO account (id) VALUES (1), (2)")
    await project.query("INSERT INTO profile (owner_id, note) VALUES (1, 'a'), (2, 'b')")
    await project.query("INSERT INTO visit (id, profile_id, extra_id) VALUES (1, 1, 2)")

    project.write_models(template.format(key_name="user", source_field=""))
    await project.cli_ok("makemigrations")
    assert "ops.RenameField(" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    project.write_models(template.format(key_name="user", source_field=', source_field="account_ref"'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT account_ref, note FROM profile ORDER BY account_ref") == [
        {"account_ref": 1, "note": "a"},
        {"account_ref": 2, "note": "b"},
    ]
    await project.query("INSERT INTO visit (id, profile_id) VALUES (2, 2)")

    await project.query("DELETE FROM visit WHERE id = 2")
    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert "owner_id" in await project.column_types("profile")
    assert await project.query("SELECT id, profile_id, extra_id FROM visit") == [
        {"id": 1, "profile_id": 1, "extra_id": 2}
    ]
    for later_migration in project.migration_files()[1:]:
        (project.root / "cli_rt_app" / "migrations" / f"{later_migration}.py").unlink()
    project.write_models(template.format(key_name="owner", source_field=""))
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_referenced_primary_key_column_rename_keeps_the_foreign_keys_valid(project_factory) -> None:
    project = await project_factory()
    template = """
    class Author(Model):
        code = fields.IntField(primary_key=True{source_field})
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books")
    """
    project.write_models(template.format(source_field=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (code) VALUES (1)")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 1)")

    project.write_models(template.format(source_field=', source_field="author_code"'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO author (author_code) VALUES (2)")
    await project.query("INSERT INTO book (id, author_id) VALUES (2, 2)")

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT code FROM author ORDER BY code") == [{"code": 1}, {"code": 2}]
    assert await project.query("SELECT id, author_id FROM book ORDER BY id") == [
        {"id": 1, "author_id": 1},
        {"id": 2, "author_id": 2},
    ]


@pytest.mark.asyncio
async def test_renamed_to_field_target_keeps_the_relation_pointing_at_it(project_factory) -> None:
    project = await project_factory()
    template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        {code_name} = fields.CharField(max_length=10, unique=True{source_field})
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books", to_field="{code_name}")
    """
    project.write_models(template.format(code_name="code", source_field=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id, code) VALUES (1, 'a')")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 'a')")

    project.write_models(template.format(code_name="slug", source_field=', source_field="code"'))
    await project.cli_ok("makemigrations")
    assert "ops.RenameField(" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query("SELECT id, author_id FROM book") == [{"id": 1, "author_id": "a"}]


@pytest.mark.asyncio
async def test_to_field_target_replaced_without_rename_repoints_the_relation_before_removing_it(
    project_factory,
) -> None:
    """A unique field a to_field relation points at, renamed without source_field, is detected as
    AddField + RemoveField - the relation's AlterField has to run before the RemoveField, or the
    RemoveField finds the old field still referenced."""
    project = await project_factory()
    template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        {code_name} = fields.CharField(max_length=10, unique=True)
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books", to_field="{code_name}", null=True)
    """
    project.write_models(template.format(code_name="code"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(template.format(code_name="ident"))
    await project.cli_ok("makemigrations")
    latest_migration = project.read_latest_migration()
    assert latest_migration.index("ops.AlterField(") < latest_migration.index("ops.RemoveField(")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO author (id, ident) VALUES (1, 'a')")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 'a')")
    if DatabaseUnderTest.get_dialect().supports_foreign_keys:
        with pytest.raises(Exception, match=r"(?i)foreign key"):
            await project.query("INSERT INTO book (id, author_id) VALUES (2, 'missing')")

    await project.query("DELETE FROM book")
    await project.query("DELETE FROM author")
    await project.cli_ok("migrate", "app", project.migration_files()[0])
    author_columns = await project.column_types("author")
    assert "code" in author_columns
    assert "ident" not in author_columns
    await project.query("INSERT INTO author (id, code) VALUES (1, 'a')")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 'a')")
    if DatabaseUnderTest.get_dialect().supports_foreign_keys:
        with pytest.raises(Exception, match=r"(?i)foreign key"):
            await project.query("INSERT INTO book (id, author_id) VALUES (2, 'missing')")


@pytest.mark.asyncio
async def test_to_field_target_removed_with_the_referencing_model_is_removed_after_it(project_factory) -> None:
    project = await project_factory()
    author = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=10)
    """
    book = """
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("app.Author", related_name="books", to_field="code", null=True)
    """
    project.write_models(author + "    code = fields.CharField(max_length=10, unique=True)\n" + book)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(author)
    await project.cli_ok("makemigrations")
    latest_migration = project.read_latest_migration()
    assert latest_migration.index("ops.DeleteModel(") < latest_migration.index("ops.RemoveField(")
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert "code" in await project.column_types("author")
    assert "author_id" in await project.column_types("book")


def get_migration_dependencies_line(migration_source: str) -> str:
    return migration_source.split("dependencies = ", 1)[1].split("\n", 1)[0]


@pytest.mark.asyncio
async def test_to_field_target_replaced_in_another_app_is_removed_in_a_follow_up_migration(project_factory) -> None:
    project = await project_factory("a", "b")
    author_template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        {code_name} = fields.CharField(max_length=10, unique=True)
    """
    book_template = """
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("a.Author", related_name="books", to_field="{code_name}", null=True)
    """
    project.write_models(author_template.format(code_name="code"), "a")
    project.write_models(book_template.format(code_name="code"), "b")
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(author_template.format(code_name="ident"), "a")
    project.write_models(book_template.format(code_name="ident"), "b")
    await project.cli_ok("makemigrations")
    a_migrations = project.migration_files("a")
    b_migrations = project.migration_files("b")
    assert len(a_migrations) == 3
    follow_up_migration = project.read_latest_migration("a")
    assert "ops.RemoveField(" in follow_up_migration
    assert f'("b", "{b_migrations[-1]}")' in get_migration_dependencies_line(follow_up_migration)
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO author (id, ident) VALUES (1, 'a')")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 'a')")

    await project.query("DELETE FROM book")
    await project.query("DELETE FROM author")
    await project.cli_ok("migrate", "b", b_migrations[0])
    await project.cli_ok("migrate", "a", a_migrations[0])
    author_columns = await project.column_types("author")
    assert "code" in author_columns
    assert "ident" not in author_columns


@pytest.mark.asyncio
async def test_to_field_target_removal_waits_for_the_other_app_repointing_the_relation(project_factory) -> None:
    project = await project_factory("a", "b")
    author_template = """
    class Author(Model):
        id = fields.IntField(primary_key=True)
        slug = fields.CharField(max_length=10, unique=True)
    {code_line}
    """
    book_template = """
    class Book(Model):
        id = fields.IntField(primary_key=True)
        author = fields.ForeignKeyField("a.Author", related_name="books", to_field="{to_field}", null=True)
    """
    project.write_models(
        author_template.format(code_line="    code = fields.CharField(max_length=10, unique=True)"), "a"
    )
    project.write_models(book_template.format(to_field="code"), "b")
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(author_template.format(code_line=""), "a")
    project.write_models(book_template.format(to_field="slug"), "b")
    only_a_result = await project.cli("makemigrations", "a")
    assert only_a_result.exit_code == 1
    assert "still points at it through to_field" in only_a_result.output
    assert project.migration_files("a") == ["0001_initial"]

    await project.cli_ok("makemigrations")
    b_migrations = project.migration_files("b")
    removal_migration = project.read_latest_migration("a")
    assert f'("b", "{b_migrations[-1]}")' in get_migration_dependencies_line(removal_migration)
    await project.cli_ok("migrate")
    await project.assert_in_sync()

    await project.cli_ok("migrate", "a", "0001_initial")
    await project.cli_ok("migrate", "b", "0001_initial")
    assert "code" in await project.column_types("author")
