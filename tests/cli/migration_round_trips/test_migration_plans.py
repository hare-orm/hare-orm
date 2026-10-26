"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of the plan of several apps' migrations: models
moving between apps, the order apps are migrated and unapplied in, squashed and irreversible migrations."""

from __future__ import annotations

import re

import pytest


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
    await project.cli_ok("makemigrations", "--rename", "a.Tag=b.Tag")
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


@pytest.mark.parametrize(
    ("first_models", "second_models", "message"),
    [
        pytest.param(
            (
                "class Tag(Model):\n    id = fields.IntField(primary_key=True)\n",
                "class Other(Model):\n    id = fields.IntField(primary_key=True)\n",
            ),
            (
                "",
                "class Other(Model):\n    id = fields.IntField(primary_key=True)\n"
                "class Tag(Model):\n    id = fields.IntField(primary_key=True)\n"
                "    name = fields.CharField(max_length=20, default='')\n",
            ),
            "is moved to b.Tag",
            id="model_moved_between_apps_with_changes_is_refused_without_writing_migrations",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_makemigrations_writes_no_migration(
    project_factory, first_models, second_models, message
) -> None:
    project = await project_factory("a", "b")
    for models_text, app in zip(first_models, ("a", "b"), strict=True):
        project.write_models(models_text, app)
    await project.cli_ok("makemigrations")

    for models_text, app in zip(second_models, ("a", "b"), strict=True):
        project.write_models(models_text, app)
    result = await project.cli("makemigrations", "--rename", "a.Tag=b.Tag")

    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output
    assert project.migration_files("a") == ["0001_initial"]
    assert project.migration_files("b") == ["0001_initial"]


@pytest.mark.asyncio
async def test_a_relation_moved_to_an_existing_model_of_the_app_deleting_its_old_target(project_factory) -> None:
    """b's new relation points at a model a's existing migrations made - b's migration needs only those, so
    a's migration deleting the old target can wait for b's."""
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
    await project.cli_ok("migrate")
    await project.query("INSERT INTO keep (id) VALUES (1)")
    await project.query("INSERT INTO book (id) VALUES (1)")

    project.write_models("class Keep(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    keep = fields.ForeignKeyField("a.Keep", related_name="books", null=True)\n',
        "b",
    )
    await project.cli_ok("makemigrations")
    a_migration = project.read_latest_migration("a")
    b_migration = project.read_latest_migration("b")
    assert '("b", ' in a_migration.split("dependencies = ", 1)[1].split("\n", 1)[0]
    assert '("a", "0001_initial")' in b_migration.split("dependencies = ", 1)[1].split("\n", 1)[0]
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, keep_id FROM book") == [{"id": 1, "keep_id": None}]
    await project.cli_ok("migrate", "a", "0001_initial")
    await project.cli_ok("migrate", "b", "0001_initial")


@pytest.mark.asyncio
async def test_an_app_unapplied_after_the_app_depending_on_it_runs_its_code_without_that_apps_models(
    project_factory,
) -> None:
    """b's Book comes before a's data migration in the plan; `migrate a zero` unapplied b first, then ran a's
    reverse code on the state cached before - Book still in it: deleting an author cascaded into its dropped
    table."""
    project = await project_factory("a", "b")
    project.write_models("class Author(Model):\n    id = fields.IntField(primary_key=True)\n", "a")
    project.write_models(
        "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    author = fields.ForeignKeyField("a.Author", related_name="books", null=True)\n',
        "b",
    )
    await project.cli_ok("makemigrations")
    project.write_migration(
        "0002_authors",
        """
        from hare import migrations
        from hare.migrations import operations as ops


        async def add_authors(apps, schema_editor):
            await apps.get_model("a.Author").objects.create(id=1)


        async def remove_authors(apps, schema_editor):
            await apps.get_model("a.Author").objects.all().delete()


        class Migration(migrations.Migration):
            dependencies = [("a", "0001_initial")]
            operations = [ops.RunPython(add_authors, remove_authors)]
        """,
        "a",
    )
    project.write_migration(
        "0002_after_authors",
        """
        from hare import migrations


        class Migration(migrations.Migration):
            dependencies = [("b", "0001_initial"), ("a", "0002_authors")]
            operations = []
        """,
        "b",
    )
    await project.cli_ok("migrate")
    await project.query("INSERT INTO book (id, author_id) VALUES (1, 1)")

    await project.cli_ok("migrate", "a", "zero")
    tables = await project.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('author', 'book')"
        if not project.is_postgres
        else "SELECT table_name AS name FROM information_schema.tables WHERE table_name IN ('author', 'book') "
        "AND table_schema = current_schema()"
    )
    assert tables == []


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
async def test_sqlmigrate_prints_a_squashed_migration_and_the_ones_it_replaces(project_factory) -> None:
    """sqlmigrate read the graph a squashed migration had taken over: a replaced migration - one migrate
    still applies while only some are applied - was "Cannot find migration"."""
    project = await project_factory()
    models = """
    class Item(Model):
        id = fields.IntField(primary_key=True, generated=False)
        qty = fields.IntField()
    """
    for added_field in ("", "tag = fields.IntField(null=True)\n", "tag2 = fields.IntField(null=True)\n"):
        models = models.rstrip(" ") + (f"        {added_field}" if added_field else "")
        project.write_models(models)
        await project.cli_ok("makemigrations")
    initial, tag_migration, tag2_migration = project.migration_files()
    await project.cli_ok("migrate", "app", tag_migration)
    await project.cli_ok("squashmigrations", "app", tag2_migration)
    squashed = next(name for name in project.migration_files() if "_squashed_" in name)

    for name, expected_sql in (
        (initial, "CREATE TABLE"),
        (tag_migration, "ADD COLUMN"),
        (tag2_migration, "ADD COLUMN"),
        (squashed, "CREATE TABLE"),
    ):
        output = await project.cli_ok("sqlmigrate", "app", name)
        assert expected_sql in output, (name, output)
    # The column quoted as the database quotes it.
    quoted_tag2 = re.compile(r"[\"`]tag2[\"`]")
    assert quoted_tag2.search(await project.cli_ok("sqlmigrate", "app", tag2_migration))
    assert quoted_tag2.search(await project.cli_ok("sqlmigrate", "app", squashed))


@pytest.mark.asyncio
async def test_a_rollback_through_an_irreversible_migration_is_refused_before_anything_is_undone(
    project_factory,
) -> None:
    """Unapplying to a target behind an irreversible migration undid the later reversible migrations -
    their reverse SQL ran - and only then stopped at the irreversible one, half way."""
    project = await project_factory()
    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        label = fields.CharField(max_length=20)
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, label) VALUES (1, 'first')")
    project.write_migration(
        "0002_irreversible",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        async def mark(apps, schema_editor):
            pass

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RunPython(mark)]
        """,
    )
    project.write_migration(
        "0003_reversible",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0002_irreversible")]
            operations = [
                ops.RunSQL("UPDATE item SET label = 'forward'", reverse_sql="UPDATE item SET label = 'reversed'")
            ]
        """,
    )
    await project.cli_ok("migrate")

    result = await project.cli("migrate", "app", "zero")

    assert result.exit_code not in (0, None)
    assert "is not reversible" in result.output
    assert await project.query("SELECT label FROM item") == [{"label": "forward"}]
    names = {row["name"] for row in await project.query("SELECT name FROM hare_migrations")}
    assert {"0002_irreversible", "0003_reversible"} <= names
