"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of many-to-many relations: automatic through
tables, through models and the rows they hold."""

from __future__ import annotations

import textwrap

import pytest

from tests.utils.database_under_test import DatabaseUnderTest


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
    if DatabaseUnderTest.pools_server_sessions(project.database_url):
        pytest.skip("a transaction pooler's open server sessions keep the database's old search path")
    database_name = (await project.query("SELECT current_database() AS name"))[0]["name"]
    # A current schema other than "public" - a table moved out of a schema goes back there.
    await project.query("CREATE SCHEMA app_space")
    await project.query(f'ALTER DATABASE "{database_name}" SET search_path TO app_space')
    # The database's settings apply to the sessions opened after them.
    await project.close_query_context()
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
async def test_a_through_table_keeps_its_index_names_through_a_key_rename_and_a_model_rename(project_factory) -> None:
    project = await project_factory()
    template = """
    class {model}(Model):
        a = fields.IntField()
        {component} = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "{component}")
        peers = fields.ManyToManyField("app.{model}", related_name="peered", through="node_peers")
    """
    project.write_models(textwrap.dedent(template.format(model="Node", component="b")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    steps = [
        ("Node", "c", 'ops.RenameField(model_name="Node", old_name="b", new_name="c")'),
        ("Vertex", "c", 'ops.RenameModel(old_name="Node", new_name="Vertex")'),
    ]
    for model, component, operation in steps:
        project.write_models(textwrap.dedent(template.format(model=model, component=component)))
        previous = project.migration_files()[-1]
        project.write_migration(
            f"{len(project.migration_files()) + 1:04d}_step",
            f"""
from hare import migrations
from hare.migrations import operations as ops

class Migration(migrations.Migration):
    dependencies = [("app", "{previous}")]
    operations = [{operation}]
""",
        )
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "0001_initial")


@pytest.mark.asyncio
async def test_a_through_table_follows_a_key_rename_of_a_renamed_target_back_and_forth(project_factory) -> None:
    project = await project_factory()
    target = """
    class {name}(Model):
        id = fields.IntField()
        {key} = fields.IntField()
        pk = fields.CompositePrimaryKey("id", "{key}")
    """
    holder = """
    class Holder(Model):
        id = fields.IntField(primary_key=True)
        links = fields.ManyToManyField("app.{name}", related_name="holders", through="holder_links")
    """

    def write_step(operation: str) -> str:
        previous = project.migration_files()[-1]
        project.write_migration(
            f"{len(project.migration_files()) + 1:04d}_step",
            f"""
from hare import migrations
from hare.migrations import operations as ops

class Migration(migrations.Migration):
    dependencies = [("app", "{previous}")]
    operations = [{operation}]
""",
        )
        return previous

    project.write_models(textwrap.dedent(target.format(name="Item", key="k1")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(textwrap.dedent(target.format(name="Part", key="k1")))
    write_step('ops.RenameModel(old_name="Item", new_name="Part")')
    await project.cli_ok("migrate")
    project.write_models(textwrap.dedent(target.format(name="Part", key="k1") + holder.format(name="Part")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(textwrap.dedent(target.format(name="Part", key="k2") + holder.format(name="Part")))
    previous = write_step('ops.RenameField(model_name="Part", old_name="k1", new_name="k2")')
    await project.cli_ok("migrate")
    await project.cli_ok("migrate", "app", previous)
    await project.cli_ok("migrate")
    await project.assert_in_sync()


THROUGH_MODELS = """
class Tag(Model):
    id = fields.IntField(primary_key=True)
{owner}
class BookTag(Model):
    id = fields.IntField(primary_key=True)
{owner_key}    tag = fields.ForeignKeyField("app.Tag", related_name="book_tags")
"""


THROUGH_OWNER = "class Book(Model):\n    id = fields.IntField(primary_key=True)\n"


THROUGH_RELATION = '    tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.BookTag")\n'


THROUGH_OWNER_KEY = '    book = fields.ForeignKeyField("app.Book", related_name="book_tags")\n'


@pytest.mark.asyncio
async def test_a_removed_relation_through_a_model_leaves_that_models_table_and_rows(project_factory) -> None:
    project = await project_factory()
    project.write_models(THROUGH_MODELS.format(owner=THROUGH_OWNER + THROUGH_RELATION, owner_key=THROUGH_OWNER_KEY))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id) VALUES (1)")
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO booktag (id, book_id, tag_id) VALUES (1, 1, 1)")
    project.write_models(THROUGH_MODELS.format(owner=THROUGH_OWNER, owner_key=THROUGH_OWNER_KEY))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT book_id, tag_id FROM booktag") == [{"book_id": 1, "tag_id": 1}]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT book_id, tag_id FROM booktag") == [{"book_id": 1, "tag_id": 1}]
    drift_result = await project.cli("drift", "app")
    assert "booktag" not in drift_result.output.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "through"), [("Book", "BookTag"), ("Zebra", "Alink"), ("Abook", "Zlink")], ids=str)
@pytest.mark.parametrize("owner_is_deleted", [True, False])
async def test_a_relation_through_a_model_goes_before_the_key_of_that_model_it_reads(
    project_factory, owner: str, through: str, owner_is_deleted: bool
) -> None:
    project = await project_factory()
    template = """
    class Tag(Model):
        id = fields.IntField(primary_key=True)
    {owner_source}
    class {through}(Model):
        id = fields.IntField(primary_key=True)
        {owner_key}
        tag = fields.ForeignKeyField("app.Tag", related_name="links")
    """
    owner_source = f"class {owner}(Model):\n        id = fields.IntField(primary_key=True)\n"
    relation = f'        tags = fields.ManyToManyField("app.Tag", related_name="owners", through="app.{through}")\n'
    project.write_models(
        textwrap.dedent(
            template.format(
                owner_source=owner_source + relation,
                through=through,
                owner_key=f'owner = fields.ForeignKeyField("app.{owner}", related_name="links")',
            )
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(
        textwrap.dedent(
            template.format(owner_source="" if owner_is_deleted else owner_source, through=through, owner_key="")
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_model_relating_to_itself_through_a_table_is_rolled_back(project_factory) -> None:
    project = await project_factory()
    project.write_models(
        """
        class Person(Model):
            id = fields.IntField(primary_key=True)
            friends = fields.ManyToManyField("app.Person", related_name="friend_of", through="friendship")
        """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_renamed_through_model_keeps_the_relation_going_through_it(project_factory) -> None:
    project = await project_factory()
    template = """
    class Tag(Model):
        id = fields.IntField(primary_key=True, generated=False)

    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        tags = fields.ManyToManyField("app.Tag", related_name="books", through="app.{through}")

    class {through}(Model):
        id = fields.IntField(primary_key=True, generated=False)
        book = fields.ForeignKeyField("app.Book", related_name="links")
        tag = fields.ForeignKeyField("app.Tag", related_name="links")
    """
    project.write_models(textwrap.dedent(template.format(through="BookTag")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO tag (id) VALUES (1)")
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO booktag (id, book_id, tag_id) VALUES (1, 1, 1)")
    project.write_models(textwrap.dedent(template.format(through="Tagging")))
    project.write_migration(
        "0002_rename_through",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameModel(old_name="BookTag", new_name="Tagging")]
        """,
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT book_id, tag_id FROM tagging") == [{"book_id": 1, "tag_id": 1}]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT book_id, tag_id FROM booktag") == [{"book_id": 1, "tag_id": 1}]
