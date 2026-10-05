"""A connection to a ClickHouse cluster (its ``cluster`` setting): the schema created and changed on
every server, a table distributed over the shards read, written, changed and remade through its
distributed table, a replicated table and the journal of migrations read on every server."""

import os

import pytest

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.clickhouse_cluster_statements import ClickhouseClusterStatements
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.operations import AddField, AlterModelOptions, CreateModel, DeleteModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.cluster.models import Site, Visit

DISTRIBUTED = {"visit": "visit_local"}


def get_statements(statement, replicates_schema=False):
    return ClickhouseClusterStatements.get_statements(
        statement, "hare_sharded", DISTRIBUTED, replicates_schema=replicates_schema
    )


def test_statements_run_on_every_server_and_where_the_rows_are():
    assert get_statements('CREATE TABLE "site" ("name" String) ENGINE = MergeTree ORDER BY name') == [
        'CREATE TABLE "site" ON CLUSTER hare_sharded ("name" String) ENGINE = MergeTree ORDER BY name'
    ]
    assert get_statements('CREATE TABLE IF NOT EXISTS db."visit_local" ("id" Int64)') == [
        'CREATE TABLE IF NOT EXISTS db."visit_local" ON CLUSTER hare_sharded ("id" Int64)'
    ]
    # A change of columns goes to both tables, the local one first; a change of storage and of rows
    # to the local table alone.
    assert get_statements('ALTER TABLE "visit" ADD COLUMN "n" Int32') == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded ADD COLUMN "n" Int32',
        'ALTER TABLE "visit" ON CLUSTER hare_sharded ADD COLUMN "n" Int32',
    ]
    assert get_statements('ALTER TABLE "visit" MODIFY COLUMN "n" Int32 CODEC(ZSTD)') == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded MODIFY COLUMN "n" Int32 CODEC(ZSTD)'
    ]
    assert get_statements('ALTER TABLE "visit" ADD INDEX "ix" ("n") TYPE minmax GRANULARITY 1') == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded ADD INDEX "ix" ("n") TYPE minmax GRANULARITY 1'
    ]
    assert get_statements('ALTER TABLE "visit" UPDATE "n" = 1 WHERE 1', replicates_schema=True) == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded UPDATE "n" = 1 WHERE 1'
    ]
    assert get_statements('DELETE FROM "visit" WHERE "id" = 1') == [
        'DELETE FROM "visit_local" ON CLUSTER hare_sharded WHERE "id" = 1'
    ]
    # The rows of a table of no distributed one change where they are sent - a replicated table
    # copies the change itself.
    assert get_statements('DELETE FROM "site" WHERE 1') == ['DELETE FROM "site" WHERE 1']
    assert get_statements('DROP TABLE "visit"') == ['DROP TABLE "visit" ON CLUSTER hare_sharded']
    assert get_statements('TRUNCATE TABLE "visit"') == ['TRUNCATE TABLE "visit_local" ON CLUSTER hare_sharded']
    assert get_statements('RENAME TABLE "a" TO "b";') == ['RENAME TABLE "a" TO "b" ON CLUSTER hare_sharded']
    assert get_statements('SYSTEM RELOAD DICTIONARY "d"') == ['SYSTEM RELOAD DICTIONARY ON CLUSTER hare_sharded "d"']
    # A database replicating its schema itself is sent the change alone.
    assert get_statements('ALTER TABLE "site" ADD COLUMN "n" Int32', replicates_schema=True) == [
        'ALTER TABLE "site" ADD COLUMN "n" Int32'
    ]
    for statement in ("SELECT 1", 'INSERT INTO "visit" VALUES (1)', 'CREATE TABLE "t" ON CLUSTER c ("a" Int8)'):
        assert get_statements(statement) == [statement]


def test_a_distributed_table_is_checked():
    with pytest.raises(ConfigurationError):
        ClickhouseTableOptions(sharding_key=RawSQLTerm("id"))
    with pytest.raises(ConfigurationError):
        ClickhouseTableOptions(distributed_over=" ")


@pytest.mark.asyncio
async def test_a_distributed_table_spreads_its_rows_and_changes_them_where_they_are(clickhouse_cluster_db):
    connection = Visit._meta.connection
    await Visit.objects.bulk_create([Visit(id=number, site="ab"[number % 2], duration=number) for number in range(10)])
    assert await Visit.objects.count() == 10
    # Each server holds a part of the rows.
    local_counts = await connection.execute_dicts(
        "SELECT hostName() AS host, count() AS rows FROM clusterAllReplicas('hare_sharded', currentDatabase(), "
        "visit_local) GROUP BY host ORDER BY host"
    )
    assert len(local_counts) == 2 and all(row["rows"] > 0 for row in local_counts)
    assert sum(row["rows"] for row in local_counts) == 10

    assert await Visit.objects.filter(site="a").update(duration=0) == 5
    assert await Visit.objects.filter(duration=0).count() == 5
    assert await Visit.objects.filter(id__lt=4).delete() == 4
    assert await Visit.objects.order_by("id").values_list("id", flat=True) == [4, 5, 6, 7, 8, 9]
    visit = await Visit.objects.get(id=5)
    visit.duration = 50
    await visit.save()
    assert (await Visit.objects.get(id=5)).duration == 50


@pytest.mark.asyncio
async def test_a_replicated_table_is_kept_on_every_server(clickhouse_cluster_db):
    connection = Site._meta.connection
    await Site.objects.bulk_create([Site(name="a", owner="ann"), Site(name="b", owner="bob")])
    await Site.objects.filter(name="a").update(owner="amy")
    await connection.execute_script("SYSTEM SYNC REPLICA site")
    rows = await connection.execute_dicts(
        "SELECT hostName() AS host, groupArray(owner) AS owners FROM clusterAllReplicas('hare_sharded', "
        "currentDatabase(), site) GROUP BY host ORDER BY host"
    )
    assert [sorted(row["owners"]) for row in rows] == [["amy", "bob"], ["amy", "bob"]]


@pytest.mark.asyncio
async def test_migrations_change_the_schema_on_every_server(clickhouse_cluster_db):
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    distributed = ClickhouseTableOptions(distributed_over="order_local", sharding_key=RawSQLTerm("id"))
    await CreateModel(
        name="Order",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("total", fields.IntField())],
        options={"table": "order", "table_options": [distributed]},
    ).run("models", state, dry_run=False, state_editor=editor)

    async def get_tables():
        rows = await connection.execute_dicts(
            "SELECT hostName() AS host, name, engine FROM clusterAllReplicas('hare_sharded', system.tables) "
            "WHERE database = currentDatabase() AND name LIKE 'order%' ORDER BY host, name"
        )
        return sorted({(row["name"], row["engine"]) for row in rows}), len({row["host"] for row in rows})

    try:
        assert await get_tables() == ([("order", "Distributed"), ("order_local", "MergeTree")], 2)
        await connection.execute_script('INSERT INTO "order" VALUES (1, 10), (2, 20), (3, 30), (4, 40)')
        await AddField(model_name="Order", name="note", field=fields.CharField(max_length=10, default="-")).run(
            "models", state, dry_run=False, state_editor=editor
        )
        rows = await connection.execute_dicts('SELECT id, total, note FROM "order" ORDER BY id')
        assert [(row["id"], row["total"], row["note"]) for row in rows] == [
            (1, 10, "-"),
            (2, 20, "-"),
            (3, 30, "-"),
            (4, 40, "-"),
        ]
        # Another sort remakes the local tables, the rows copied each to its shard.
        await AlterModelOptions(
            name="Order",
            options={
                "table": "order",
                "table_options": [
                    ClickhouseTableOptions(
                        order_by=("id", "total"), distributed_over="order_local", sharding_key=RawSQLTerm("id")
                    )
                ],
            },
        ).run("models", state, dry_run=False, state_editor=editor)
        rows = await connection.execute_dicts('SELECT id, total FROM "order" ORDER BY id')
        assert [(row["id"], row["total"]) for row in rows] == [(1, 10), (2, 20), (3, 30), (4, 40)]
        sorting_keys = await connection.execute_dicts(
            "SELECT DISTINCT sorting_key FROM clusterAllReplicas('hare_sharded', system.tables) "
            "WHERE database = currentDatabase() AND name = 'order_local'"
        )
        assert [row["sorting_key"] for row in sorting_keys] == ["id, total"]
        assert await get_tables() == ([("order", "Distributed"), ("order_local", "MergeTree")], 2)
    finally:
        await DeleteModel(name="Order").run("models", state, dry_run=False, state_editor=editor)
    assert await get_tables() == ([], 0)


@pytest.mark.asyncio
async def test_a_table_of_scattered_rows_is_not_remade(clickhouse_cluster_db):
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Note",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("text", fields.TextField())],
        options={"table": "note"},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        with pytest.raises(UnSupportedError, match="only the rows of this server"):
            await AlterModelOptions(
                name="Note",
                options={"table": "note", "table_options": [ClickhouseTableOptions(order_by=("id", "text"))]},
            ).run("models", state, dry_run=False, state_editor=editor)
    finally:
        await DeleteModel(name="Note").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_the_journal_of_migrations_is_read_on_every_server(clickhouse_cluster_db):
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    recorder = MigrationRecorder(connection, table_name="hare_cluster_migrations")
    await recorder.ensure_schema(editor)
    try:
        engines = await connection.execute_dicts(
            "SELECT DISTINCT engine FROM clusterAllReplicas('hare_sharded', system.tables) "
            "WHERE database = currentDatabase() AND name = 'hare_cluster_migrations'"
        )
        assert [row["engine"] for row in engines] == ["ReplicatedMergeTree"]
        await recorder.record_applied("models", "0001_initial")
        # Each server holds the record, once the other one has fetched it.
        await connection.execute_script(
            f'SYSTEM SYNC REPLICA ON CLUSTER hare_sharded "{connection.database}".hare_cluster_migrations'
        )
        rows = await connection.execute_dicts(
            "SELECT hostName() AS host, groupArray(name) AS names FROM clusterAllReplicas('hare_sharded', "
            "currentDatabase(), hare_cluster_migrations) GROUP BY host"
        )
        assert [row["names"] for row in rows] == [["0001_initial"], ["0001_initial"]]
        assert [key.name for key in await recorder.applied_migrations()] == ["0001_initial"]
    finally:
        await connection.execute_script('DROP TABLE "hare_cluster_migrations" SYNC')


@pytest.mark.asyncio
async def test_a_replicated_database_replicates_the_schema_itself(clickhouse_cluster_db):
    connection = Visit._meta.connection
    database = f"{connection.database}_replicated"
    await connection.execute_script(
        f'CREATE DATABASE "{database}" ON CLUSTER hare_sharded '
        f"ENGINE = Replicated('/hare/{database}', '{{shard}}', '{{replica}}')"
    )
    config = DbUrlConfigGenerator.expand(os.environ["HARE_TEST_CLICKHOUSE_CLUSTER_DB"].replace("{}", "x"))
    client = type(connection)(connection_alias="replicated", **{**config["credentials"], "database": database})
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        state = State(models={}, apps=StateApps())
        await CreateModel(
            name="Tag",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("label", fields.TextField())],
            options={"table": "tag", "table_options": [ClickhouseTableOptions(engine="ReplicatedMergeTree")]},
        ).run("models", state, dry_run=False, state_editor=editor)
        # Sent without ON CLUSTER, which the database refuses - it runs the DDL on its servers itself.
        assert await client.cluster.fetch_replicates_schema()
        await AddField(model_name="Tag", name="weight", field=fields.IntField(default=1)).run(
            "models", state, dry_run=False, state_editor=editor
        )
        rows = await client.execute_dicts(
            "SELECT hostName() AS host, groupArray(name) AS columns FROM clusterAllReplicas('hare_sharded', "
            f"system.columns) WHERE database = '{database}' AND table = 'tag' GROUP BY host"
        )
        assert [sorted(row["columns"]) for row in rows] == [["id", "label", "weight"], ["id", "label", "weight"]]
    finally:
        await client.close()
        await connection.execute_script(f'DROP DATABASE "{database}" ON CLUSTER hare_sharded SYNC')
