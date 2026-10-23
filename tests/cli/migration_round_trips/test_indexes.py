"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of indexes and unique constraints: plain,
expression, descending and concurrently built ones."""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from hare.exceptions import IntegrityError
from tests.utils.cli_project import CliProject
from tests.utils.database_under_test import DatabaseUnderTest


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


async def get_index_names(project: CliProject, table: str) -> list[str]:
    if project.is_postgres:
        # Not the primary key's index.
        rows = await project.query(
            "SELECT index_class.relname AS name FROM pg_index "
            "JOIN pg_class index_class ON index_class.oid = pg_index.indexrelid "
            "JOIN pg_class table_class ON table_class.oid = pg_index.indrelid "
            f"WHERE table_class.relname = '{table}' "
            "AND table_class.relnamespace = current_schema()::regnamespace AND NOT pg_index.indisprimary"
        )
    else:
        rows = await project.query(
            f"SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = '{table}' AND sql IS NOT NULL"
        )
    return sorted(row["name"] for row in rows)


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
        class Item(Model):
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
async def test_an_index_keyed_by_an_ordered_expression_stays_in_sync(project_factory) -> None:
    project = await project_factory()
    header = (
        "from hare.ddl.indexes import Index\n"
        "from hare.query.expressions import F\n"
        "from hare.query.functions import Lower\n"
    )
    steps = [
        """
        class Item(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            rank = fields.IntField(default=0)
            class Meta:
                indexes = [Index(Lower("name").desc(), F("rank").asc(), name="item_lower_name")]
        """,
        """
        class Item(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=50)
            rank = fields.IntField(default=0)
            class Meta:
                indexes = [Index(Lower("name").asc(), F("rank").asc(), name="item_lower_name")]
        """,
    ]
    for source in steps:
        project.write_models(header + textwrap.dedent(source))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        assert await get_index_names(project, "item") == ["item_lower_name"]
    assert len(project.migration_files()) == 2


@pytest.mark.asyncio
async def test_an_index_keyed_by_a_dialect_function_round_trips(project_factory) -> None:
    project = await project_factory()
    header = (
        "from hare.ddl import Index\n"
        "from hare.query.expressions import F\n"
        "from hare.query.functions import Cast\n"
        "from hare.query.functions.datetime.extract import Extract\n"
        "from hare.query.functions.datetime.trunc import Trunc\n"
    )
    source = """
    class Stamp(Model):
        id = fields.IntField(primary_key=True)
        made = fields.DateField(null=True)
        ratio = fields.FloatField(null=True)
        data = fields.JSONField(null=True)
        class Meta:
            indexes = [{indexes}]
    """
    json_index = 'Index(F("data__key"))'
    function_indexes = (
        'Index(Trunc("made", "month"), name="stamp_month"), Index(Trunc("made", "day")), '
        'Index(Extract("made", "year")), Index(Cast("ratio", fields.IntField()))'
    )
    if not project.is_postgres:
        # hare's own SQLite functions exist on its connections only - a key needing one is refused.
        project.write_models(header + textwrap.dedent(source.format(indexes='Index(Trunc("made", "month"))')))
        await project.cli_ok("makemigrations")
        refused = await project.cli("migrate")
        assert refused.exit_code not in (0, None)
        assert "hare_trunc_date_part" in refused.output and "hare's own connections" in refused.output
        project = await project_factory()
        function_indexes = ""
    project.write_models(header + textwrap.dedent(source.format(indexes=f"{json_index}, {function_indexes}")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO stamp (id, made, ratio) VALUES (1, '2026-05-03', 2.5)")
    assert len(await get_index_names(project, "stamp")) == (5 if project.is_postgres else 1)
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.parametrize(
    ("imports", "name_field", "indexes"),
    [
        pytest.param(
            "Lower, Upper",
            "name = fields.CharField(max_length=50)",
            'Index(Lower("name"), unique=True, name="item_lower_unique"), Index(Upper("name"))',
            id="a_unique_expression_index_beside_another_expression_index_stays_in_sync",
        ),
        pytest.param(
            "Lower",
            "name = fields.CharField(max_length=50, null=True)",
            'Index(Lower("name")), Index(Lower("name"), name="aa_lower_named")',
            id="an_unnamed_and_a_named_index_of_one_shape_stay_in_sync",
        ),
    ],
)
@pytest.mark.asyncio
async def test_two_expression_indexes_stay_in_sync(project_factory, imports, name_field, indexes) -> None:
    project = await project_factory()
    project.write_models(
        f"from hare.ddl import Index\nfrom hare.query.functions import {imports}\n"
        "class Item(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        f"    {name_field}\n"
        "    class Meta:\n"
        f"        indexes = [{indexes}]\n"
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_field_unique_flag_beside_a_named_unique_constraint_of_its_column(project_factory) -> None:
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        code = fields.IntField(null=True{unique})
        class Meta:
            constraints = [UniqueConstraint(fields=("code",), name="item_code_unique")]
    """
    for unique in ("", ", unique=True", ""):
        project.write_models(textwrap.dedent(template.format(unique=unique)))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    await project.query("INSERT INTO item (id, code) VALUES (1, 5)")
    if DatabaseUnderTest.get_dialect(project.database_url).name in ("postgresql", "sqlite"):
        with pytest.raises(IntegrityError):
            await project.query("INSERT INTO item (id, code) VALUES (2, 5)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_options", "indexes"),
    [
        ("", 'Index(fields=("code",)), Index(fields=("-code",))'),
        ("", 'Index(fields=("code",), name="up"), Index(fields=("-code",), name="down")'),
        (", db_index=True", 'Index(fields=("-code",))'),
    ],
    ids=["unnamed", "named", "field_index"],
)
async def test_a_descending_index_beside_a_plain_index_of_its_column_stays_in_sync(
    project_factory, field_options: str, indexes: str
) -> None:
    project = await project_factory()
    project.write_models(
        f"""
        from hare.ddl import Index

        class Item(Model):
            id = fields.IntField(primary_key=True)
            code = fields.IntField(null=True{field_options})

            class Meta:
                indexes = [{indexes}]
        """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "indexes",
    [
        pytest.param('Index(RawSQLTerm("code"), name="ix_code")', id="bare"),
        pytest.param('Index(RawSQLTerm(\'"code"\'), name="ix_code")', id="double_quoted"),
        pytest.param("Index(RawSQLTerm('`code`'), name=\"ix_code\")", id="backquoted"),
        pytest.param('Index(RawSQLTerm("(code)"), name="ix_code")', id="parenthesized"),
        pytest.param(
            'Index(RawSQLTerm("code"), name="ix_code"), Index(RawSQLTerm("label"), name="ix_label")', id="two"
        ),
        pytest.param(
            'Index(RawSQLTerm("code"), name="ix_code"), Index(fields=("code",), name="ix_field")',
            id="beside_a_field_index",
        ),
        pytest.param('Index(RawSQLTerm("code"), name="ix_code", unique=True)', id="unique"),
        pytest.param(
            'Index(RawSQLTerm("code"), RawSQLTerm("label"), name="ix_code", unique=True)', id="unique_of_two"
        ),
    ],
)
async def test_an_index_of_a_raw_key_naming_one_column_stays_in_sync(project_factory, indexes: str) -> None:
    project = await project_factory()
    if "`" in indexes and project.is_postgres:
        pytest.skip("PostgreSQL quotes a name in double quotes only")
    if "unique=True" in indexes and DatabaseUnderTest.get_dialect(project.database_url).name == "clickhouse":
        pytest.skip("ClickHouse has no unique indexes")
    project.write_models(
        f"""
        from hare.ddl import Index, RawSQLTerm

        class Item(Model):
            id = fields.IntField(primary_key=True)
            code = fields.IntField(default=0)
            label = fields.IntField(default=0)

            class Meta:
                indexes = [{indexes}]
        """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields_text", ['"parent", "label"', '"label", "-parent"'], ids=["relation_first", "descending"]
)
async def test_an_index_over_a_relation_to_a_key_of_two_columns_stays_in_sync(
    project_factory, fields_text: str
) -> None:
    project = await project_factory()
    project.write_models(
        f"""
        from hare.ddl import Index

        class Parent(Model):
            a = fields.IntField()
            b = fields.CharField(max_length=5)
            pk = fields.CompositePrimaryKey("a", "b")

        class Child(Model):
            id = fields.IntField(primary_key=True)
            parent = fields.ForeignKeyField("app.Parent", related_name="children")
            label = fields.CharField(max_length=10)

            class Meta:
                indexes = [Index(fields=({fields_text}), name="child_parent_label")]
        """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_a_concurrently_built_index_is_added_and_removed_by_migrate(project_factory) -> None:
    """CREATE INDEX CONCURRENTLY waits for every transaction older than itself - the one holding the
    migration lock among them: migrate waited for itself forever."""
    project = await project_factory()
    project.write_models(
        "class Item(Model):\n    id = fields.IntField(primary_key=True)\n    code = fields.CharField(max_length=20)\n"
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, code) VALUES (1, 'a')")
    project.write_models(
        "from hare.ddl import Index\n"
        "class Item(Model):\n    id = fields.IntField(primary_key=True)\n    code = fields.CharField(max_length=20)\n"
        '    class Meta:\n        indexes = [Index(fields=("code", "id"), name="item_code_idx")]\n'
    )
    project.write_migration(
        "0002_item_code_idx",
        """
        from hare import migrations
        from hare.ddl import Index
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            atomic = False
            dependencies = [("app", "0001_initial")]
            operations = [
                ops.AddIndex(
                    model_name="Item", index=Index(fields=("code", "id"), name="item_code_idx"), concurrently=True
                )
            ]
        """,
    )

    await asyncio.wait_for(project.cli_ok("migrate"), 30)

    await project.assert_in_sync()
    await asyncio.wait_for(project.cli_ok("migrate", "app", "0001_initial"), 30)
    await asyncio.wait_for(project.cli_ok("migrate"), 30)
    await project.assert_in_sync()


@pytest.mark.asyncio
async def test_a_field_index_declared_in_meta_indexes_too_is_one_index(project_factory) -> None:
    """migrate created the Meta.indexes entry over an indexed field again under the field index's name."""
    project = await project_factory()
    template = """
    from hare.ddl import Index

    class Doc(Model):
        id = fields.IntField(primary_key=True, generated=False)
        title = fields.CharField(max_length=30, db_index={db_index})

        class Meta:
            indexes = {indexes}
    """
    steps = [
        ("True", "()"),
        ("True", '(Index(fields=("title",)),)'),
        ("False", '(Index(fields=("title",)),)'),
        ("True", "()"),
    ]
    for db_index, indexes in steps:
        project.write_models(textwrap.dedent(template.format(db_index=db_index, indexes=indexes)))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()

    await project.cli_ok("migrate", "app", project.migration_files()[0])
    project.write_models(textwrap.dedent(template.format(db_index="True", indexes="()")))
    await project.cli_ok("drift", "app")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
