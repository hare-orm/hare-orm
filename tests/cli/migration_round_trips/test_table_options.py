"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of the options of a model's table: managed,
descriptions, views and partitioning."""

from __future__ import annotations

import textwrap

import pytest


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
async def test_a_view_of_a_queryset_is_made_without_a_connection(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("views exist on PostgreSQL and ClickHouse")
    template = """
    from hare.ddl import View
    from hare.query.queryset import ModelQuery

    class Invoice(Model):
        id = fields.IntField(primary_key=True, generated=False)
        paid = fields.BooleanField(default=False)
        amount = fields.{amount_type}()

        class Meta:
            views = [View("paid_invoices", query=ModelQuery().filter(paid=True).values("id", "amount"))]
    """
    for amount_type in ("IntField", "BigIntField"):
        project.write_models(template.format(amount_type=amount_type))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    await project.query("INSERT INTO invoice (id, paid, amount) VALUES (1, true, 5), (2, false, 6)")
    assert await project.query("SELECT id, amount FROM paid_invoices") == [{"id": 1, "amount": 5}]
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_a_partitioned_table_moves_to_another_schema_with_its_partitions(project_factory) -> None:
    """The partitions stayed in the schema the table left, and the migration dropping that schema
    dropped them - with every row."""
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("partitioned tables are PostgreSQL's")
    template = """
    from hare.dialects.postgresql.partitioning import HashPartitioning
    from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions

    class Sale(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        amount = fields.IntField()
        class Meta:
            schema = "{schema}"
            table_options = [PostgresqlTableOptions(partitioning=HashPartitioning(fields=("id",), partition_count=3))]
    """
    project.write_models(textwrap.dedent(template.format(schema="sales")))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(
        "INSERT INTO sales.sale (id, amount) SELECT number, number * 10 FROM generate_series(1, 9) number"
    )
    project.write_models(textwrap.dedent(template.format(schema="archive")))
    await project.cli_ok("makemigrations")

    await project.cli_ok("migrate")

    await project.assert_in_sync()
    assert await project.query("SELECT count(*) AS rows, sum(amount) AS amount FROM archive.sale") == [
        {"rows": 9, "amount": 450}
    ]
    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT count(*) AS rows FROM sales.sale") == [{"rows": 9}]
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT count(*) AS rows FROM archive.sale") == [{"rows": 9}]
