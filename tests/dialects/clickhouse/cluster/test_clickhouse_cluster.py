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
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.exceptions import ConfigurationError, OperationalError, UnSupportedError
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.operations import AddField, AlterModelOptions, CreateModel, DeleteModel
from hare.migrations.state.state import State
from hare.query.expressions import Exists, OuterReference
from hare.query.functions import Count
from tests.dialects.clickhouse.cluster.models import Click, Label, Site, Visit
from tests.utils.operations import apply_operations

DISTRIBUTED = {("test", "visit"): "visit_local"}


def get_statements(statement, replicates_schema=False):
    return ClickhouseClusterStatements.get_statements(
        statement, "hare_sharded", DISTRIBUTED, database="test", replicates_schema=replicates_schema
    )


@pytest.mark.database_independent
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


@pytest.mark.database_independent
def test_a_column_change_rewriting_its_rows_leaves_the_rewrite_out_of_the_distributed_table():
    """A column made nullable rewrites its rows in the same ALTER - sent to the distributed table too,
    which takes no mutation (code 48), and the migration stuck."""
    statement = 'ALTER TABLE "visit" MODIFY COLUMN "n" Nullable(Int32), UPDATE "n" = "n" WHERE 1'
    assert get_statements(statement) == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded MODIFY COLUMN "n" Nullable(Int32), '
        'UPDATE "n" = "n" WHERE 1',
        'ALTER TABLE "visit" ON CLUSTER hare_sharded MODIFY COLUMN "n" Nullable(Int32)',
    ]
    # Text that only looks like the rewrite stays.
    comment = """ALTER TABLE "visit" MODIFY COLUMN "n" Int32 COMMENT 'a, UPDATE b = b WHERE 1 or not'"""
    assert get_statements(comment)[1] == comment.replace('"visit"', '"visit" ON CLUSTER hare_sharded')


@pytest.mark.database_independent
def test_a_name_holding_its_quote_takes_on_cluster_after_it():
    """ON CLUSTER went inside a name holding a doubled quote, and the server refused the statement."""
    assert get_statements('CREATE TABLE "sr""c t" ("id" Int32) ENGINE = Log') == [
        'CREATE TABLE "sr""c t" ON CLUSTER hare_sharded ("id" Int32) ENGINE = Log'
    ]
    assert get_statements('DROP TABLE db."a\\"b"') == ['DROP TABLE db."a\\"b" ON CLUSTER hare_sharded']
    assert get_statements("ALTER TABLE `a``b` DROP COLUMN x") == [
        "ALTER TABLE `a``b` ON CLUSTER hare_sharded DROP COLUMN x"
    ]
    statement = 'ALTER TABLE "visit" MODIFY COLUMN "c""d" Nullable(Int32), UPDATE "c""d" = "c""d" WHERE 1'
    assert get_statements(statement) == [
        'ALTER TABLE "visit_local" ON CLUSTER hare_sharded MODIFY COLUMN "c""d" Nullable(Int32), '
        'UPDATE "c""d" = "c""d" WHERE 1',
        'ALTER TABLE "visit" ON CLUSTER hare_sharded MODIFY COLUMN "c""d" Nullable(Int32)',
    ]
    assert ClickhouseClusterStatements.get_name_parts('db."sr""c t"') == ("db.", 'sr"c t')
    assert ClickhouseClusterStatements.get_local_name('"vi""sit"', 'lo"cal') == '"lo""cal"'


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("argument_sql", "name"),
    [("'v l'", "v l"), ("'v\\\\l'", "v\\l"), ("'it\\'s'", "it's"), ("v_local", "v_local"), ("`v l`", "v l")],
)
def test_the_local_table_of_a_distributed_one_is_read_in_any_quoting(argument_sql, name):
    """A local table named with a space was taken for none - the distributed table read back as a plain one; a
    backslash in its name stayed escaped, and a change of the table named a table that didn't exist."""
    assert ClickhouseSqlParts.get_name_argument(argument_sql) == name
    assert ClickhouseClusterStatements.get_local_name('"d"', name).startswith('"')


@pytest.mark.database_independent
def test_words_inside_a_string_neither_name_the_cluster_nor_the_storage():
    """A comment reading "ON CLUSTER" kept the statement to the server it was sent to - the other servers
    were left without the change; one reading "TTL" kept a column's comment from the distributed table."""
    statement = """ALTER TABLE "site" COMMENT COLUMN "n" 'runs ON CLUSTER x here'"""
    assert get_statements(statement) == [statement.replace('"site"', '"site" ON CLUSTER hare_sharded')]
    statement = """ALTER TABLE "visit" COMMENT COLUMN "n" 'kept by TTL here'"""
    assert get_statements(statement) == [
        statement.replace('"visit"', '"visit_local" ON CLUSTER hare_sharded'),
        statement.replace('"visit"', '"visit" ON CLUSTER hare_sharded'),
    ]


@pytest.mark.database_independent
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

    # ClickHouse reports no count of the rows a mutation changes - the rows show it.
    assert await Visit.objects.filter(site="a").update(duration=0) is None
    assert await Visit.objects.filter(duration=0).count() == 5
    # A visit's labels are deleted with it by hare, which counts the visits it read for them.
    assert await Visit.objects.filter(id__lt=4).delete() == 4
    assert await Visit.objects.order_by("id").values_list("id", flat=True) == [4, 5, 6, 7, 8, 9]
    visit = await Visit.objects.get(id=5)
    visit.duration = 50
    await visit.save()
    assert (await Visit.objects.get(id=5)).duration == 50


@pytest.mark.asyncio
async def test_a_distributed_table_joins_and_reads_subqueries_of_the_rows_of_every_shard(clickhouse_cluster_db):
    """A read of a Distributed table runs on every shard - a table it joins and a subquery of its IN are
    read once, GLOBAL, and sent to them: the rows of another shard, and of a table kept on the server
    the query is sent to, are all seen."""
    connection = Visit._meta.connection
    await Visit.objects.bulk_create([Visit(id=number, site="a") for number in range(1, 9)])
    await Click.objects.bulk_create([Click(id=number, visit_id=(number % 8) + 1) for number in range(1, 17)])
    shards = await connection.execute_dicts(
        "SELECT hostName() AS host, count() AS rows FROM clusterAllReplicas('hare_sharded', currentDatabase(), "
        "visit_local) GROUP BY host"
    )
    assert len(shards) == 2
    label = await Label.objects.create(name="tagged")
    tagged = await Visit.objects.filter(id__in=[2, 3, 5, 7])
    await label.visits.add(*tagged)

    assert sorted(visit.id for visit in await label.visits.all()) == [2, 3, 5, 7]
    assert await Visit.objects.filter(labels__name="tagged").count() == 4
    assert await Visit.objects.filter(id__in=Label.objects.values("visits__id")).count() == 4
    assert await Visit.objects.exclude(id__in=Label.objects.values("visits__id")).count() == 4
    assert await Visit.objects.filter(id__in=Click.objects.filter(id__gt=8).values("visit_id")).count() == 8
    clicked = Click.objects.filter(visit=OuterReference("pk"), id__gt=12)
    assert await Visit.objects.filter(Exists(clicked)).count() == 4
    counts = (
        await Visit.objects.annotate(clicks_count=Count("clicks")).order_by("id").values_list("id", "clicks_count")
    )
    assert counts == [(number, 2) for number in range(1, 9)]
    assert await Click.objects.filter(visit__site="a").count() == 16


@pytest.mark.asyncio
async def test_a_replicated_table_is_kept_on_every_server(clickhouse_cluster_db):
    connection = Site._meta.connection
    await Site.objects.bulk_create([Site(name="a", owner="ann"), Site(name="b", owner="bob")])
    await Site.objects.filter(name="a").update(owner="amy")
    # A bulk update reads its rows' keys as a subquery of their own values - one a replicated table
    # takes as the same on every replica.
    await Site.objects.bulk_update([Site(name="b", owner="bea")], fields=["owner"])
    await connection.execute_script("SYSTEM SYNC REPLICA site")
    rows = await connection.execute_dicts(
        "SELECT hostName() AS host, groupArray(owner) AS owners FROM clusterAllReplicas('hare_sharded', "
        "currentDatabase(), site) GROUP BY host ORDER BY host"
    )
    assert [sorted(row["owners"]) for row in rows] == [["amy", "bea"], ["amy", "bea"]]


@pytest.mark.asyncio
async def test_migrations_change_the_schema_on_every_server(clickhouse_cluster_db):
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    distributed = ClickhouseTableOptions(distributed_over="order_local", sharding_key=RawSQLTerm("id"))
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Order",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("total", fields.IntField())],
            options={"table": "order", "table_options": [distributed]},
        ),
        schema_editor=editor,
    )

    async def get_tables():
        rows = await connection.execute_dicts(
            "SELECT hostName() AS host, name, engine FROM clusterAllReplicas('hare_sharded', system.tables) "
            "WHERE database = currentDatabase() AND name LIKE 'order%' ORDER BY host, name"
        )
        return sorted({(row["name"], row["engine"]) for row in rows}), len({row["host"] for row in rows})

    try:
        assert await get_tables() == ([("order", "Distributed"), ("order_local", "MergeTree")], 2)
        await connection.execute_script('INSERT INTO "order" VALUES (1, 10), (2, 20), (3, 30), (4, 40)')
        await apply_operations(
            "models",
            state,
            AddField(model_name="Order", name="note", field=fields.CharField(max_length=10, default="-")),
            schema_editor=editor,
        )
        rows = await connection.execute_dicts('SELECT id, total, note FROM "order" ORDER BY id')
        assert [(row["id"], row["total"], row["note"]) for row in rows] == [
            (1, 10, "-"),
            (2, 20, "-"),
            (3, 30, "-"),
            (4, 40, "-"),
        ]
        # Another sort remakes the local tables, the rows copied each to its shard.
        await apply_operations(
            "models",
            state,
            AlterModelOptions(
                name="Order",
                options={
                    "table": "order",
                    "table_options": [
                        ClickhouseTableOptions(
                            order_by=("id", "total"), distributed_over="order_local", sharding_key=RawSQLTerm("id")
                        )
                    ],
                },
            ),
            schema_editor=editor,
        )
        rows = await connection.execute_dicts('SELECT id, total FROM "order" ORDER BY id')
        assert [(row["id"], row["total"]) for row in rows] == [(1, 10), (2, 20), (3, 30), (4, 40)]
        sorting_keys = await connection.execute_dicts(
            "SELECT DISTINCT sorting_key FROM clusterAllReplicas('hare_sharded', system.tables) "
            "WHERE database = currentDatabase() AND name = 'order_local'"
        )
        assert [row["sorting_key"] for row in sorting_keys] == ["id, total"]
        assert await get_tables() == ([("order", "Distributed"), ("order_local", "MergeTree")], 2)
    finally:
        await apply_operations("models", state, DeleteModel(name="Order"), schema_editor=editor)
    assert await get_tables() == ([], 0)


@pytest.mark.asyncio
async def test_a_table_of_scattered_rows_is_not_remade(clickhouse_cluster_db):
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Note",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("text", fields.TextField())],
            options={"table": "note"},
        ),
        schema_editor=editor,
    )
    try:
        with pytest.raises(UnSupportedError, match="only the rows of this server"):
            await apply_operations(
                "models",
                state,
                AlterModelOptions(
                    name="Note",
                    options={"table": "note", "table_options": [ClickhouseTableOptions(order_by=("id", "text"))]},
                ),
                schema_editor=editor,
            )
    finally:
        await apply_operations("models", state, DeleteModel(name="Note"), schema_editor=editor)


@pytest.mark.asyncio
async def test_a_replicated_table_of_rows_per_shard_is_not_remade(clickhouse_cluster_db):
    """A replicated table counted as complete on every server - on a cluster of several shards whose
    replica path names the shard each shard keeps rows of its own, and a remake would copy the rows of
    the one it runs on."""
    connection = Visit._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State()
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Ledger",
            fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("text", fields.TextField())],
            options={"table": "ledger", "table_options": [ClickhouseTableOptions(engine="ReplicatedMergeTree")]},
        ),
        schema_editor=editor,
    )
    try:
        with pytest.raises(UnSupportedError, match="keep rows of their own on each shard"):
            await apply_operations(
                "models",
                state,
                AlterModelOptions(
                    name="Ledger",
                    options={
                        "table": "ledger",
                        "table_options": [
                            ClickhouseTableOptions(engine="ReplicatedMergeTree", order_by=("id", "text"))
                        ],
                    },
                ),
                schema_editor=editor,
            )
    finally:
        await apply_operations("models", state, DeleteModel(name="Ledger"), schema_editor=editor)


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
        state = State()
        await apply_operations(
            "models",
            state,
            CreateModel(
                name="Tag",
                fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("label", fields.TextField())],
                options={"table": "tag", "table_options": [ClickhouseTableOptions(engine="ReplicatedMergeTree")]},
            ),
            schema_editor=editor,
        )
        # Sent without ON CLUSTER, which the database refuses - it runs the DDL on its servers itself.
        assert await client.cluster.fetch_replicates_schema()
        await apply_operations(
            "models",
            state,
            AddField(model_name="Tag", name="weight", field=fields.IntField(default=1)),
            schema_editor=editor,
        )
        rows = await client.execute_dicts(
            "SELECT hostName() AS host, groupArray(name) AS columns FROM clusterAllReplicas('hare_sharded', "
            f"system.columns) WHERE database = '{database}' AND table = 'tag' GROUP BY host"
        )
        assert [sorted(row["columns"]) for row in rows] == [["id", "label", "weight"], ["id", "label", "weight"]]
    finally:
        await client.close()
        await connection.execute_script(f'DROP DATABASE "{database}" ON CLUSTER hare_sharded SYNC')


@pytest.mark.asyncio
async def test_drift_reads_a_distributed_and_a_replicated_table(clickhouse_cluster_db):
    """Drift read the Distributed table, not the local ones holding the rows: every distributed model
    showed changed options and an untracked local table, and a change of a local table was hidden;
    a ReplicatedMergeTree declared without arguments differed from the ones the server gave it."""
    from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
    from hare.migrations.drift import detect_drift

    connection = Visit._meta.connection
    state = MigrationAutodetector(
        clickhouse_cluster_db.apps, {"models": {"models": [], "default_connection": "models"}}
    ).current_state()

    async def get_drift():
        result = await detect_drift(connection, state, ["models"])
        return (
            sorted(f"{type(operation).__name__} {getattr(operation, 'name', '')}" for operation in result.operations),
            [(mismatch.table, mismatch.column) for mismatch in result.mismatched_columns],
            result.untracked_tables,
        )

    assert await get_drift() == ([], [], [])
    on_cluster = " ON CLUSTER hare_sharded"
    await connection.execute_script(f"ALTER TABLE visit_local{on_cluster} MODIFY COLUMN duration Int64")
    try:
        assert await get_drift() == (["AlterField duration"], [], [])
    finally:
        await connection.execute_script(f"ALTER TABLE visit_local{on_cluster} MODIFY COLUMN duration Int32")
    assert await get_drift() == ([], [], [])


@pytest.mark.asyncio
async def test_a_distributed_table_over_unreplicated_tables_on_replicas_is_refused(clickhouse_cluster_db):
    """On a cluster whose shard has two replicas, a plain MergeTree kept every row on the replica it was
    written to, and a read through the Distributed table could ask the other one."""
    from hare.core.connections.connections import Connections

    connection = Connections.current().create_independent("models", {"cluster": "hare_replicated"})
    try:
        editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
        state = State()
        with pytest.raises(UnSupportedError, match="shards have several replicas"):
            await apply_operations(
                "models",
                state,
                CreateModel(
                    name="Sale",
                    fields=[("id", fields.BigIntField(primary_key=True, generated=False))],
                    options={
                        "table": "sale",
                        "table_options": [
                            ClickhouseTableOptions(distributed_over="sale_local", sharding_key=RawSQLTerm("id"))
                        ],
                    },
                ),
                schema_editor=editor,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_generate_schemas_refuses_a_distributed_table_over_unreplicated_tables_on_replicas(
    clickhouse_cluster_db,
):
    """generate_schemas() created what a migration refuses - local MergeTree tables on two replicas of a
    shard, the rows written through one of them missing from the other."""
    from hare.core.connections.connections import Connections

    connection = Connections.current().create_independent("models", {"cluster": "hare_replicated"})
    try:
        with pytest.raises(UnSupportedError, match="shards have several replicas"):
            await connection.generate_schema(safe=True)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_script_of_the_user_runs_as_written_and_one_hare_builds_on_every_server(clickhouse_cluster_db):
    connection = Visit._meta.connection

    async def get_servers_of_table(table_name):
        rows = await connection.execute_dicts(
            "SELECT count() AS servers FROM clusterAllReplicas('hare_sharded', system.tables) "
            f"WHERE database = currentDatabase() AND name = '{table_name}'"
        )
        return rows[0]["servers"]

    try:
        await connection.execute_script('CREATE TABLE "written_as_is" ("id" Int64) ENGINE = MergeTree ORDER BY "id"')
        assert await get_servers_of_table("written_as_is") == 1
        await connection.execute_built_script(
            'CREATE TABLE "built_by_hare" ("id" Int64) ENGINE = MergeTree ORDER BY "id"'
        )
        assert await get_servers_of_table("built_by_hare") == 2
    finally:
        await connection.execute_script('DROP TABLE IF EXISTS "written_as_is"')
        await connection.execute_built_script('DROP TABLE IF EXISTS "built_by_hare"')


@pytest.mark.asyncio
async def test_a_distributed_table_of_another_database_spreads_and_changes_its_rows_there(clickhouse_cluster_db):
    from hare import Hare
    from hare.models import Model

    connection = Visit._meta.connection
    database = f"{connection.database}_other"
    await connection.execute_script(f'CREATE DATABASE IF NOT EXISTS "{database}" ON CLUSTER hare_sharded')
    registered = []
    try:

        class OtherVisit(Model):
            id = fields.BigIntField(primary_key=True, generated=False)
            duration = fields.IntField(default=0)

            class Meta:
                table = "other_visit"
                schema = database
                table_options = [
                    ClickhouseTableOptions(
                        distributed_over="other_visit_local", sharding_key=RawSQLTerm("intHash64(id)")
                    )
                ]

        Hare.register_live_models([OtherVisit], app_label="other_models", connection_alias="models")
        registered.append(OtherVisit)
        await connection.dialect.schema_editor_class(connection).create_model(OtherVisit)
        # A table of the local table's name in the connection's database is no part of it.
        await connection.execute_script(
            'CREATE TABLE "other_visit_local" ON CLUSTER hare_sharded (id Int64) ENGINE = MergeTree ORDER BY id'
        )
        await OtherVisit.objects.bulk_create([OtherVisit(id=number, duration=number) for number in range(10)])
        local_counts = await connection.execute_dicts(
            f"SELECT hostName() AS host, count() AS rows FROM clusterAllReplicas('hare_sharded', '{database}', "
            "other_visit_local) GROUP BY host ORDER BY host"
        )
        assert len(local_counts) == 2 and sum(row["rows"] for row in local_counts) == 10

        await OtherVisit.objects.filter(id__lt=5).update(duration=0)
        assert await OtherVisit.objects.filter(duration=0).count() == 5
        await OtherVisit.objects.filter(id__gte=8).delete()
        assert await OtherVisit.objects.order_by("id").values_list("id", flat=True) == list(range(8))
    finally:
        Hare.unregister_live_models(registered)
        await connection.execute_script(f'DROP DATABASE "{database}" ON CLUSTER hare_sharded SYNC')
        await connection.execute_script('DROP TABLE IF EXISTS "other_visit_local" ON CLUSTER hare_sharded SYNC')


@pytest.mark.asyncio
async def test_a_statement_failing_on_one_server_names_the_server_s_error(clickhouse_cluster_db):
    """Over HTTP the server broke its response off after the status of the server that succeeded - the
    error read as an unfinished response, its text lost. The servers of the test cluster replicate one
    shard: the second one adds the column the first one's replication already added."""
    connection = Site._meta.connection
    await connection.execute_script(
        'CREATE TABLE "replicated_twice" ON CLUSTER hare_sharded (id Int64) ENGINE = ReplicatedMergeTree ORDER BY id'
    )
    try:
        with pytest.raises(OperationalError, match="DUPLICATE_COLUMN"):
            await connection.execute_script(
                'ALTER TABLE "replicated_twice" ON CLUSTER hare_sharded ADD COLUMN "added" Nullable(Int32)'
            )
    finally:
        await connection.execute_script('DROP TABLE IF EXISTS "replicated_twice" ON CLUSTER hare_sharded SYNC')
