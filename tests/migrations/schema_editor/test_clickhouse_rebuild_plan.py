from unittest.mock import AsyncMock

import pytest

from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.view import View
from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.clickhouse_cluster import ClickhouseCluster
from hare.dialects.clickhouse.cluster.constants import CLICKHOUSE_CLUSTER_HOSTS_SQL
from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_REBUILD_TARGET_SQL
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.migrations.operations import AlterModelOptions, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.utils.fake_client import FakeClient

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.fixture
def rebuild_model():
    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        title = fields.TextField()

        class Meta:
            app = "schema_plan"
            table = "book"
            table_options = [ClickhouseTableOptions(engine="MergeTree")]
            views = [View("book_titles", RawSQLTerm("SELECT id, title FROM book"))]

    apps = StateApps()
    apps.register_model("schema_plan", Book)
    apps.init_relations()
    return Book


@pytest.mark.parametrize("engine", ["Atomic", "Replicated", "Shared"])
@pytest.mark.parametrize("failure", [None, "INSERT", "EXCHANGE", "CREATE VIEW"])
async def test_rebuild_preserves_original_until_exchange_and_releases_it_after_finalization(
    rebuild_model,
    engine,
    failure,
    monkeypatch,
):
    client = FakeClient("clickhouse")
    client.cluster = None
    client.database = "default"
    editor = ClickhouseSchemaEditor(client, database_engines={"default": engine})
    plan = editor.table_rebuild.get_rebuild_plan(rebuild_model, rebuild_model)
    preview = ClickhouseSchemaEditor(client, collect_sql=True, database_engines={"default": engine})
    await plan.execute(preview)
    statements = preview.collected_sql
    exchange = next(sql for sql in statements if sql.startswith("EXCHANGE"))
    replacement = exchange.split(" AND ")[1]
    release = "DROP TABLE " + replacement
    copy = next(sql for sql in statements if sql.startswith("INSERT"))
    drop_view = next(sql for sql in statements if sql.startswith("DROP VIEW"))
    assert (
        statements.index(copy) < statements.index(drop_view) < statements.index(exchange) < statements.index(release)
    )
    assert any(sql.startswith("-- On failure") for sql in statements)
    trace = []

    async def execute(sql, **kwargs):
        trace.append(sql)
        if failure is not None and sql.startswith(failure):
            raise ValueError(failure)

    monkeypatch.setattr(editor, "run_sql", execute)
    if failure is None:
        await plan.execute(editor)
        assert trace[-1] == release
        assert trace.count(exchange) == 1
    else:
        with pytest.raises(ValueError, match=failure):
            await plan.execute(editor)
        if failure == "INSERT":
            assert trace[-1] == release
            assert not any(sql.startswith(("DROP VIEW", "EXCHANGE")) for sql in trace)
        else:
            assert release not in trace
    assert 'DROP TABLE "book"' not in trace


@pytest.mark.parametrize("snapshot", [None, {"default": "Ordinary"}])
async def test_unsupported_exchange_database_fails_before_ddl(rebuild_model, snapshot, monkeypatch):
    client = FakeClient("clickhouse")
    client.cluster = None
    client.database = "default"
    catalog = AsyncMock(return_value=[{"engine": "Ordinary"}])
    monkeypatch.setattr(client, "execute_dicts", catalog)
    editor = ClickhouseSchemaEditor(client, database_engines=snapshot)
    execute = AsyncMock()
    monkeypatch.setattr(editor, "run_sql", execute)
    with pytest.raises(UnSupportedError, match="EXCHANGE TABLES"):
        await editor.table_rebuild.get_rebuild_plan(rebuild_model, rebuild_model).execute(editor)
    execute.assert_not_called()
    assert catalog.await_count == (1 if snapshot is None else 0)


@pytest.mark.parametrize("engine", ["Atomic", "Replicated", "Shared"])
@pytest.mark.parametrize(
    "previous,current", [(None, "book storage"), ("old storage", "new storage"), ("book storage", None)]
)
async def test_storage_transition_copies_the_previous_logical_table_and_preserves_source_until_exchange(
    engine,
    previous,
    current,
    monkeypatch,
):
    state = State()
    CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True, generated=False)), ("title", fields.TextField())],
        options={
            "table": "book",
            "table_options": [ClickhouseTableOptions(engine="MergeTree", distributed_over=previous)],
        },
    ).state_forward("schema_plan", state)
    target = state.clone()
    AlterModelOptions(
        "Book",
        {"table_options": [ClickhouseTableOptions(engine="MergeTree", distributed_over=current)]},
    ).state_forward("schema_plan", target)
    old_model = state.apps.get_model("schema_plan.Book")
    new_model = target.apps.get_model("schema_plan.Book")
    client = FakeClient("clickhouse")
    client.database = "default"
    client.cluster = ClickhouseCluster(client, "alpha")
    editor = ClickhouseSchemaEditor(client, database_engines={"default": engine})
    trace = []

    async def catalog(sql, values=None):
        if sql == CLICKHOUSE_REBUILD_TARGET_SQL:
            return []
        assert sql == CLICKHOUSE_CLUSTER_HOSTS_SQL
        return [{"hosts": 1, "shards": 1}]

    async def execute(sql, **kwargs):
        trace.append(sql)

    monkeypatch.setattr(client, "execute_dicts", catalog)
    monkeypatch.setattr(editor, "run_sql", execute)
    plan = editor.table_partitions.get_alter_table_options_plan(old_model, new_model)
    assert plan.before.qualified_name == f'"{previous or "book"}"'
    assert plan.after.qualified_name == f'"{current or "book"}"'
    await plan.execute(editor)
    exchange = next(sql for sql in trace if sql.startswith("EXCHANGE"))
    assert exchange.startswith(f'EXCHANGE TABLES "{previous or "book"}" AND ')
    placement = ' ON CLUSTER "alpha"' if engine == "Atomic" else ""
    rename = f'RENAME TABLE "{previous or "book"}" TO "{current or "book"}"{placement}'
    assert trace.index(exchange) < trace.index(rename) < len(trace) - 1
    copy = next(sql for sql in trace if sql.startswith("INSERT"))
    assert 'FROM "book"' in copy
    companion_drop = f'DROP TABLE "book"{placement}'
    if previous is not None:
        flush = 'SYSTEM FLUSH DISTRIBUTED "default"."book" ON CLUSTER "alpha"'
        assert trace.index(flush) < trace.index(copy)
        assert trace.index(copy) < trace.index(companion_drop) < trace.index(exchange)
    else:
        assert companion_drop not in trace
    assert trace[-1].startswith("DROP TABLE ") and " rebuild " in trace[-1]
    if current is not None:
        trace.clear()

        async def occupied(sql, values=None):
            return [{"name": current}] if sql == CLICKHOUSE_REBUILD_TARGET_SQL else [{"hosts": 1, "shards": 1}]

        monkeypatch.setattr(client, "execute_dicts", occupied)
        with pytest.raises(ConfigurationError, match="already exists"):
            await editor.table_partitions.get_alter_table_options_plan(old_model, new_model).execute(editor)
        assert trace == []


@pytest.mark.parametrize("database_engine", ["Atomic", "Replicated", "Shared"])
@pytest.mark.parametrize("cluster_name", [None, "alpha"])
@pytest.mark.parametrize("failure", [None, "SYSTEM FLUSH"])
async def test_router_rebuild_drains_all_queues_without_copying_remote_rows(
    database_engine, cluster_name, failure, monkeypatch
):
    state = State()
    CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True, generated=False))],
        options={
            "table": "book",
            "table_options": [
                ClickhouseTableOptions(engine=ClickhouseEngine("Distributed", "remote", "archive", "books"))
            ],
        },
    ).state_forward("schema_plan", state)
    model = state.apps.get_model("schema_plan.Book")
    client = FakeClient("clickhouse")
    client.database = "default"
    client.cluster = None if cluster_name is None else ClickhouseCluster(client, cluster_name)
    editor = ClickhouseSchemaEditor(client, database_engines={"default": database_engine})
    trace = []

    async def execute(sql, **kwargs):
        trace.append(sql)
        if failure is not None and sql.startswith(failure):
            raise ValueError(failure)

    catalog = AsyncMock(side_effect=AssertionError("unexpected catalog read"))
    monkeypatch.setattr(client, "execute_dicts", catalog)
    monkeypatch.setattr(editor, "run_sql", execute)
    plan = editor.table_rebuild.get_rebuild_plan(model, model)
    flush = 'SYSTEM FLUSH DISTRIBUTED "default"."book"' + (' ON CLUSTER "alpha"' if cluster_name else "")
    if failure is not None:
        with pytest.raises(ValueError, match=failure):
            await plan.execute(editor)
        assert trace == [flush]
    else:
        await plan.execute(editor)
        assert trace[0] == flush
        assert any(sql.startswith("EXCHANGE TABLES") for sql in trace)
        assert not any(sql.startswith("INSERT") for sql in trace)
    catalog.assert_not_called()


async def test_storage_cannot_be_implicitly_replaced_by_a_standalone_router(rebuild_model):
    class Router(Model):
        id = fields.IntField(primary_key=True, generated=False)

        class Meta:
            app = "schema_plan"
            table = "book"
            table_options = [
                ClickhouseTableOptions(engine=ClickhouseEngine("Distributed", "remote", "archive", "books"))
            ]

    client = FakeClient("clickhouse")
    client.cluster = None
    editor = ClickhouseSchemaEditor(client)
    with pytest.raises(UnSupportedError, match="explicit data migration"):
        editor.table_rebuild.get_rebuild_plan(rebuild_model, Router)
