"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of renamed fields and tables carried to what
names them: generated columns, index keys, model options, views, policies and sequences."""

from __future__ import annotations

import textwrap

import pytest

from hare.exceptions import IntegrityError
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_a_confirmed_rename_recreates_the_generated_field_rewritten_for_the_new_name(project_factory) -> None:
    """The generated field's raw SQL is the user's: rewritten in the model, it's removed before the
    rename and added after it - unapplying adds the old one back once the column has its old name."""
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        {column} = fields.IntField(db_default=5)
        qty = fields.IntField()
        total = fields.GeneratedField(expression=RawSQLTerm("{column} * qty"), output_field=fields.IntField())
    """
    project.write_models(template.format(column="price"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, price, qty) VALUES (1, 3, 4)")

    project.write_models(template.format(column="amount"))
    await project.cli_ok("makemigrations", "--rename", "app.Item.price=amount")
    migration_source = project.read_latest_migration()
    assert (
        migration_source.index("ops.RemoveField(")
        < migration_source.index("ops.RenameField(")
        < migration_source.index("ops.AddField(")
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
async def test_a_generated_field_of_an_expression_of_hare_follows_a_rename_of_a_field_it_reads(
    project_factory,
) -> None:
    """The expression names the field structurally - the rename renames it there, so the migration is the
    rename alone and the generated column keeps computing from the renamed one."""
    project = await project_factory()
    template = """
    from hare.query.expressions import F

    class Item(Model):
        id = fields.IntField(primary_key=True)
        {column} = fields.IntField()
        qty = fields.IntField()
        total = fields.GeneratedField(expression=F("{column}") * F("qty"), output_field=fields.IntField())
    """
    project.write_models(template.format(column="price"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, price, qty) VALUES (1, 3, 4)")

    project.write_models(template.format(column="amount"))
    await project.cli_ok("makemigrations", "--rename", "app.Item.price=amount")
    migration_source = project.read_latest_migration()
    assert "ops.RenameField(" in migration_source
    assert "ops.RemoveField(" not in migration_source
    assert "ops.AddField(" not in migration_source
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query("INSERT INTO item (id, amount, qty) VALUES (2, 5, 10)")
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
async def test_rename_field_rewrites_the_index_key_expressions_reading_it(project_factory) -> None:
    project = await project_factory()
    header = (
        "from hare.ddl import CheckConstraint, Index\n"
        "from hare.query.expressions import F, Q\n"
        "from hare.query.functions import Lower, Upper\n"
    )
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        {column} = fields.CharField(max_length=50)
        rank = fields.IntField(default=0)
        class Meta:
            indexes = [
                Index(Lower("{column}").desc(), F("rank").asc(), name="item_lower"),
                Index(Lower("{column}"), unique=True, name="item_lower_unique"),
                Index(Upper("{column}")),
            ]
            constraints = [CheckConstraint(check=Q({column}__gt=""), name="item_set")]
    """
    project.write_models(header + textwrap.dedent(template.format(column="title")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, title, rank) VALUES (1, 'Ab', 1)")
    project.write_models(header + textwrap.dedent(template.format(column="name")))
    project.write_migration(
        "0002_rename_title",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Item", old_name="title", new_name="name")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    if DatabaseUnderTest.get_dialect(project.database_url).name in ("postgresql", "sqlite"):
        with pytest.raises(IntegrityError):
            await project.query("INSERT INTO item (id, name, rank) VALUES (2, 'ab', 2)")

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, title FROM item") == [{"id": 1, "title": "Ab"}]


@pytest.mark.asyncio
async def test_renaming_the_soft_delete_and_lock_fields_keeps_the_options_naming_them(project_factory) -> None:
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        {deleted} = fields.DatetimeField(null=True)
        {version} = fields.IntField(default=1)
        class Meta:
            soft_delete_field = "{deleted}"
            optimistic_lock_field = "{version}"
    """
    project.write_models(textwrap.dedent(template.format(deleted="deleted_at", version="version")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    project.write_models(textwrap.dedent(template.format(deleted="removed_at", version="revision")))
    project.write_migration(
        "0002_rename_fields",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [
                ops.RenameField(model_name="Item", old_name="deleted_at", new_name="removed_at"),
                ops.RenameField(model_name="Item", old_name="version", new_name="revision"),
            ]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "0001_initial")


@pytest.mark.asyncio
async def test_renaming_a_partition_key_field_keeps_the_partitioning_naming_it(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("partitioned tables are PostgreSQL's")
    template = """
    from hare.dialects.postgresql.partitioning import HashPartitioning
    from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions

    class Unread(Model):
        {user} = fields.BigIntField()
        report = fields.BigIntField()
        pk = fields.CompositePrimaryKey("{user}", "report")
        class Meta:
            table_options = [
                PostgresqlTableOptions(partitioning=HashPartitioning(fields=("{user}",), partition_count=4))
            ]
    """
    project.write_models(textwrap.dedent(template.format(user="user_id")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO unread (user_id, report) VALUES (7, 1)")
    project.write_models(textwrap.dedent(template.format(user="reader_id")))
    project.write_migration(
        "0002_rename_user_id",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameField(model_name="Unread", old_name="user_id", new_name="reader_id")]
        """,
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT reader_id, report FROM unread") == [{"reader_id": 7, "report": 1}]


@pytest.mark.asyncio
async def test_a_renamed_field_is_renamed_in_the_views_policies_and_sequences_reading_it(project_factory) -> None:
    """The sequence and the Q policy name the field and follow the rename; the views and the raw policy
    rewritten in the model are removed before the rename and added after it."""
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("views, policies and sequences of a model are PostgreSQL's")
    template = """
    from hare.ddl import DatabaseSequence, MaterializedView, Policy, RawSQLTerm, RowLevelSecurity, View
    from hare.query.expressions import Q

    class Ledger(Model):
        id = fields.IntField(primary_key=True, generated=False)
        {name} = fields.BigIntField(null=True)

        class Meta:
            sequences = [DatabaseSequence("ledger_number", owned_by="{name}")]
            views = [View("ledger_numbers", query=RawSQLTerm("SELECT id, {name} FROM ledger"))]
            materialized_views = [
                MaterializedView(
                    "ledger_totals", query=RawSQLTerm("SELECT {name} FROM ledger"), unique_columns=("{name}",)
                )
            ]
            row_level_security = RowLevelSecurity.ENABLED
            policies = [
                Policy("positive", using=Q({name}__gt=0)),
                Policy("small", with_check=RawSQLTerm("{name} < 1000")),
            ]
    """
    project.write_models(template.format(name="number"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO ledger (id, number) VALUES (1, 5)")
    project.write_models(template.format(name="num"))
    await project.cli_ok("makemigrations", "--rename", "app.Ledger.number=num")
    assert "ops.RenameField(" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, num FROM ledger_numbers") == [{"id": 1, "num": 5}]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, number FROM ledger_numbers") == [{"id": 1, "number": 5}]
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "new_table", "class_name"),
    [
        ("ops.RenameModel(", "book", "Book"),
        ("ops.AlterModelTable(", "books", "Ledger"),
    ],
    ids=["model_renamed", "table_named"],
)
async def test_the_raw_views_rewritten_for_a_renamed_table_are_recreated_around_the_rename(
    project_factory, operation: str, new_table: str, class_name: str
) -> None:
    """The views' raw SQL is the user's: rewritten in the model for the new table, they're removed before
    the table is renamed and added after it."""
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("views exist on PostgreSQL and ClickHouse")
    template = """
    from hare.ddl import MaterializedView, RawSQLTerm, View

    class {class_name}(Model):
        id = fields.IntField(primary_key=True, generated=False)
        number = fields.IntField()

        class Meta:{table_option}
            views = [View("ledger_numbers", query=RawSQLTerm("SELECT id, number FROM {table}"))]
            materialized_views = [MaterializedView("ledger_count", query=RawSQLTerm("SELECT count(*) n FROM {table}"))]
    """
    project.write_models(template.format(class_name="Ledger", table="ledger", table_option=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO ledger (id, number) VALUES (1, 5)")
    table_option = "" if class_name == "Book" else f'\n            table = "{new_table}"'
    project.write_models(template.format(class_name=class_name, table=new_table, table_option=table_option))
    rename_answers = ("--rename", "app.Ledger=Book") if class_name == "Book" else ()
    await project.cli_ok("makemigrations", *rename_answers)
    migration_source = project.read_latest_migration()
    assert (
        migration_source.index("ops.RemoveView(")
        < migration_source.index(operation)
        < migration_source.index("ops.AddView(")
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, number FROM ledger_numbers") == [{"id": 1, "number": 5}]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT n FROM ledger_count") == [{"n": 1}]
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_a_field_renamed_over_its_column_takes_its_old_name_back(project_factory) -> None:
    """A field renamed keeping its column (source_field) and then renamed back failed makemigrations:
    the old and the new name both mapped to the column."""
    project = await project_factory()
    template = """
    class Thing(Model):
        id = fields.IntField(primary_key=True, generated=False)
        {field}
    """
    project.write_models(template.format(field="a = fields.IntField()"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO thing (id, a) VALUES (1, 5)")
    project.write_models(template.format(field='a2 = fields.IntField(source_field="a")'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    project.write_models(template.format(field="a = fields.IntField()"))
    await project.cli_ok("makemigrations")
    assert "RenameField" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT a FROM thing") == [{"a": 5}]
    await project.cli_ok("migrate", "app", "zero")
