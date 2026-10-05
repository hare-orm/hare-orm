"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of relations and the keys they read: key type
changes, renames and removals carried to the referencing columns."""

from __future__ import annotations

import textwrap

import pytest

from hare.exceptions import IntegrityError
from tests.utils.database_under_test import DatabaseUnderTest


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
async def test_an_integer_column_turned_into_a_relation_keeps_its_values(project_factory) -> None:
    """makemigrations failed on PostgreSQL (the relation's column type read as None) and on SQLite
    (the relation's index added twice), and migrate refused a plain field turned into a relation."""
    project = await project_factory()
    plain_models = """
    class Author(Model):
        id = fields.IntField(primary_key=True, generated=False)
    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        author_id = fields.IntField()
    """
    project.write_models(plain_models)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1)")
    await project.query("INSERT INTO book (id, author_id) VALUES (10, 1)")

    project.write_models("""
    class Author(Model):
        id = fields.IntField(primary_key=True, generated=False)
    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField("app.Author", related_name="books")
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, author_id FROM book") == [{"id": 10, "author_id": 1}]
    if DatabaseUnderTest.get_dialect().features.supports_foreign_keys:
        with pytest.raises(Exception, match=r"(?i)foreign key"):
            await project.query("INSERT INTO book (id, author_id) VALUES (11, 99)")

    await project.cli_ok("migrate", "app", "0001_initial")
    project.write_models(plain_models)
    await project.cli_ok("drift", "app")
    await project.query("INSERT INTO book (id, author_id) VALUES (11, 99)")
    assert await project.query("SELECT id, author_id FROM book ORDER BY id") == [
        {"id": 10, "author_id": 1},
        {"id": 11, "author_id": 99},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relation_declaration",
    [
        'fields.ForeignKeyField("app.Author", related_name="books")',
        'fields.OneToOneField("app.Author", related_name="book")',
    ],
)
async def test_a_relation_turned_back_into_an_integer_column_keeps_its_values(
    project_factory, relation_declaration: str
) -> None:
    """migrate dropped the relation's index under the name of the renamed column, not the one it had."""
    project = await project_factory()
    plain_models = """
    class Author(Model):
        id = fields.IntField(primary_key=True, generated=False)
    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        author_id = fields.IntField()
    """
    project.write_models(plain_models)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO author (id) VALUES (1)")
    await project.query("INSERT INTO book (id, author_id) VALUES (10, 1)")
    project.write_models(f"""
    class Author(Model):
        id = fields.IntField(primary_key=True, generated=False)
    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        author = {relation_declaration}
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    project.write_models(plain_models)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, author_id FROM book") == [{"id": 10, "author_id": 1}]

    await project.cli_ok("migrate", "app", project.migration_files()[1])
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, author_id FROM book") == [{"id": 10, "author_id": 1}]


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
    await project.cli_ok("makemigrations", "--rename", "app.Author=Writer")
    assert 'ops.RenameModel(old_name="Author", new_name="Writer")' in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    renamed_table = "writers" if meta else "writer"
    assert await project.query(f"SELECT id, name FROM {renamed_table}") == [{"id": 1, "name": "a"}]

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    assert await project.query(f"SELECT id, name FROM {table}") == [{"id": 1, "name": "a"}]


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
async def test_a_composite_key_with_a_relation_component_round_trips(project_factory) -> None:
    project = await project_factory()
    project.write_models(
        """
    class Book(Model):
        id = fields.IntField(primary_key=True)
    class Edition(Model):
        book = fields.ForeignKeyField("app.Book", related_name="editions")
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("book_id", "number")
    class Note(Model):
        edition = fields.ForeignKeyField("app.Edition", related_name="notes")
        position = fields.IntField()
        pk = fields.CompositePrimaryKey("edition", "position")
    """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO book (id) VALUES (1)")
    await project.query("INSERT INTO edition (book_id, number) VALUES (1, 1)")
    await project.query("INSERT INTO note (edition_book_id, edition_number, position) VALUES (1, 1, 1)")
    assert await project.query("SELECT position FROM note") == [{"position": 1}]

    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_renaming_a_composite_key_component_renames_the_columns_named_after_it(project_factory) -> None:
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        {component} = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "{component}")

    class Note(Model):
        id = fields.IntField(primary_key=True)
        pair = fields.ForeignKeyField("app.Pair", related_name="notes", null=True)
        pairs = fields.ManyToManyField("app.Pair", related_name="listed")
    """
    project.write_models(textwrap.dedent(template.format(component="b")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO pair (a, b) VALUES (1, 2)")
    await project.query("INSERT INTO note (id, pair_a, pair_b) VALUES (1, 1, 2)")
    await project.query("INSERT INTO note_pair (note_id, pair_a, pair_b) VALUES (1, 1, 2)")
    project.write_models(textwrap.dedent(template.format(component="c")))
    project.write_migration(
        "0002_rename_b",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Pair", old_name="b", new_name="c")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT pair_a, pair_c FROM note") == [{"pair_a": 1, "pair_c": 2}]
    assert await project.query("SELECT pair_a, pair_c FROM note_pair") == [{"pair_a": 1, "pair_c": 2}]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT pair_a, pair_b FROM note") == [{"pair_a": 1, "pair_b": 2}]
    assert await project.query("SELECT pair_a, pair_b FROM note_pair") == [{"pair_a": 1, "pair_b": 2}]


@pytest.mark.asyncio
async def test_renaming_a_composite_key_component_of_a_model_relating_to_itself(project_factory) -> None:
    project = await project_factory()
    template = """
    class Node(Model):
        a = fields.IntField()
        {component} = fields.IntField()
        parent = fields.ForeignKeyField("app.Node", related_name="children", null=True)
        pk = fields.CompositePrimaryKey("a", "{component}")
    """
    project.write_models(textwrap.dedent(template.format(component="b")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO node (a, b) VALUES (1, 1)")
    await project.query("INSERT INTO node (a, b, parent_a, parent_b) VALUES (1, 2, 1, 1)")
    project.write_models(textwrap.dedent(template.format(component="c")))
    project.write_migration(
        "0002_rename_b",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Node", old_name="b", new_name="c")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT a, c, parent_a, parent_c FROM node ORDER BY c") == [
        {"a": 1, "c": 1, "parent_a": None, "parent_c": None},
        {"a": 1, "c": 2, "parent_a": 1, "parent_c": 1},
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    await project.query("SELECT parent_b FROM node")


@pytest.mark.asyncio
async def test_relations_to_a_field_added_with_them_are_added_after_it(project_factory) -> None:
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Account(Model):
                id = fields.IntField(primary_key=True)

            class Booking(Model):
                id = fields.IntField(primary_key=True)
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(
        textwrap.dedent(
            """
            class Account(Model):
                id = fields.IntField(primary_key=True)
                code = fields.CharField(max_length=20, null=True, unique=True)
                alias_of = fields.ForeignKeyField("app.Account", related_name="aliases", null=True, to_field="code")

            class Booking(Model):
                id = fields.IntField(primary_key=True)
                account = fields.ForeignKeyField("app.Account", related_name="bookings", null=True, to_field="code")

            class Archive(Model):
                id = fields.IntField(primary_key=True)
                account = fields.ForeignKeyField("app.Account", related_name="archives", null=True, to_field="code")
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "0001_initial")


@pytest.mark.asyncio
async def test_a_one_to_one_relation_to_a_composite_key_is_removed(project_factory) -> None:
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        b = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "b")

    class Card(Model):
        id = fields.IntField(primary_key=True)
    {relation}
    """
    relation = 'pair = fields.OneToOneField("app.Pair", related_name="card", null=True)'
    for step in ("", relation, ""):
        project.write_models(textwrap.dedent(template.format(relation=f"    {step}" if step else "")))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_an_indexed_relation_to_a_field_added_with_it_keeps_its_index_after_it(project_factory) -> None:
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            """
            class Account(Model):
                id = fields.IntField(primary_key=True)

            class Ledger(Model):
                id = fields.IntField(primary_key=True)
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(
        "from hare.ddl import Index\n"
        + textwrap.dedent(
            """
            class Account(Model):
                id = fields.IntField(primary_key=True)

            class Ledger(Model):
                id = fields.IntField(primary_key=True)
                code = fields.CharField(max_length=20, null=True, unique=True)

            class Booking(Model):
                id = fields.IntField(primary_key=True)
                ledger = fields.ForeignKeyField("app.Ledger", related_name="bookings", null=True, to_field="code")
                class Meta:
                    indexes = [Index(fields=["ledger", "id"], name="booking_ledger_id")]
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_key_rename_renames_the_uniqueness_of_a_one_to_one_relation_to_it(project_factory) -> None:
    project = await project_factory()
    template = """
    class Node(Model):
        id = fields.IntField()
        {key} = fields.IntField()
        pk = fields.CompositePrimaryKey("id", "{key}")
        twin = fields.OneToOneField("app.Node", related_name="twin_of", null=True)
    """
    project.write_models(textwrap.dedent(template.format(key="k")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO node (id, k) VALUES (1, 1)")
    await project.query("INSERT INTO node (id, k, twin_id, twin_k) VALUES (2, 2, 1, 1)")
    project.write_models(textwrap.dedent(template.format(key="part")))
    project.write_migration(
        "0002_rename_k",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Node", old_name="k", new_name="part")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT twin_id, twin_part FROM node WHERE id = 2") == [{"twin_id": 1, "twin_part": 1}]
    if DatabaseUnderTest.get_dialect(project.database_url).name in ("postgresql", "sqlite"):
        with pytest.raises(IntegrityError):
            await project.query("INSERT INTO node (id, part, twin_id, twin_part) VALUES (3, 3, 1, 1)")
    await project.cli_ok("migrate", "app", "0001_initial")


TENANT_RELATION_MODELS = """
class Company(Model):
    id = fields.IntField(primary_key=True, generated=False)
class Doc(Model):
    id = fields.IntField(primary_key=True, generated=False)
    {field_name} = {declaration}

    class Meta:
        tenant_field = "{field_name}"
"""


@pytest.mark.asyncio
async def test_a_tenant_relation_turned_into_a_plain_field_of_its_name(project_factory) -> None:
    """makemigrations refused it - the state kept the relation's key column as the tenant field."""
    project = await project_factory()
    project.write_models(
        TENANT_RELATION_MODELS.format(
            field_name="company", declaration='fields.ForeignKeyField("app.Company", related_name="docs")'
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO company (id) VALUES (1)")
    await project.query("INSERT INTO doc (id, company_id) VALUES (10, 1)")

    project.write_models(TENANT_RELATION_MODELS.format(field_name="company", declaration="fields.IntField()"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    await project.assert_in_sync()
    assert await project.query("SELECT id, company FROM doc") == [{"id": 10, "company": 1}]


@pytest.mark.asyncio
async def test_a_renamed_tenant_relation_stays_the_tenant_field(project_factory) -> None:
    project = await project_factory()
    relation = 'fields.ForeignKeyField("app.Company", related_name="docs")'
    project.write_models(TENANT_RELATION_MODELS.format(field_name="company", declaration=relation))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO company (id) VALUES (1)")
    await project.query("INSERT INTO doc (id, company_id) VALUES (10, 1)")

    project.write_models(TENANT_RELATION_MODELS.format(field_name="owner", declaration=relation))
    await project.cli_ok("makemigrations", "--rename", "app.Doc.company=owner")
    await project.cli_ok("migrate")

    await project.assert_in_sync()
    assert await project.query("SELECT id, owner_id FROM doc") == [{"id": 10, "owner_id": 1}]
