"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of relations pointing at a unique field other
than the primary key (to_field)."""

from __future__ import annotations

import pytest

from tests.utils.database_under_test import DatabaseUnderTest


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
    if DatabaseUnderTest.get_dialect().features.supports_foreign_keys:
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
    if DatabaseUnderTest.get_dialect().features.supports_foreign_keys:
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
    if DatabaseUnderTest.get_dialect().features.supports_foreign_keys:
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
