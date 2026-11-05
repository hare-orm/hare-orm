"""Mutation placement is compiled from declarations and scoped to its connection."""

from functools import partial
from types import SimpleNamespace

import pytest

from hare import Hare, Model, fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.query.clickhouse_sql_context import ClickhouseSqlContext
from hare.dialects.clickhouse.query.declarations import ClickhouseQuery
from hare.dialects.clickhouse.schema.shadow.clickhouse_shadow_database import ClickhouseShadowDatabase
from hare.models.write.instance_writer import InstanceWriter
from hare.sql import Table

pytestmark = pytest.mark.database_independent


class Book(Model):
    id = fields.IntField(primary_key=True, generated=False)
    rating = fields.IntField()

    class Meta:
        table_options = [ClickhouseTableOptions(engine="ReplicatedMergeTree", distributed_over="book storage")]


@pytest.mark.parametrize("distributed", [False, True])
@pytest.mark.parametrize("lightweight", [False, True])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_mutation_target_comes_from_the_table_declaration(distributed, lightweight, operation):
    table = Table(
        "book",
        schema="library",
        query_class=ClickhouseQuery,
        table_options=(
            ClickhouseTableOptions(
                distributed_over="book storage" if distributed else None,
                lightweight_updates=lightweight,
            ),
        ),
    )
    context = ClickhouseSqlContext.from_context(ClickhouseQuery.SQL_CONTEXT).copy(cluster_name="library-cluster")
    if operation == "update":
        query = ClickhouseQuery.update(table).set(table.rating, 5).where(table.id == 1)
    else:
        query = ClickhouseQuery.from_(table).delete().where(table.id == 1)
    sql = query.get_sql(context)
    target = '"library"."book storage" ON CLUSTER "library-cluster"' if distributed else '"library"."book"'
    assert target in sql
    assert ("ON CLUSTER" in sql) is distributed
    assert table.get_name_without_alias() == "book"
    assert " ON CLUSTER " not in ClickhouseQuery.from_(table).select(table.id).get_sql(context)


@pytest.mark.asyncio
@pytest.mark.requires("clickhouse-connect")
async def test_same_alias_connections_keep_mutation_plans_and_instance_templates_separate():
    pytest.importorskip("clickhouse_connect")
    context = await Hare.init(
        {
            "connections": {"default": "clickhouse+clickhouse-connect://localhost/default?cluster=alpha"},
            "apps": {"placement": {"models": [__name__], "default_connection": "default"}},
        },
        connect=False,
    )
    other = None
    try:
        context.apps.bind_base_queries()
        model = context.apps.get_model("placement.Book")
        first = context.connections.get("default")
        other = first.create_independent_client({"cluster": "beta"})
        assert first.connection_alias == other.connection_alias
        for connection, cluster in ((first, "alpha"), (other, "beta"), (first, "alpha"), (other, "beta")):
            target = f'"book storage" ON CLUSTER "{cluster}"'
            query = model.objects.using(connection).filter(id=1).update(rating=2)
            assert target in query.sql()
            writer = InstanceWriter(model, connection)
            assert target in writer.templates.delete_sql
            assert target in writer.get_update_sql(["rating"], None)[0]
            bulk = model.objects.using(connection).bulk_update(
                [model(id=1, rating=2), model(id=2, rating=3)],
                fields=["rating"],
            )
            assert target in bulk.sql()
            compiler = query._get_compiler()._get_execution_query()
            compiler._make_query()
            assert compiler._statement_plan is not None
            assert target in compiler._statement_plan.sql
    finally:
        if other is not None:
            await other.close()
        await Hare.close_connections()
        context.__exit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.requires("clickhouse-connect")
async def test_shadow_drop_failure_still_releases_the_remaining_databases():
    client = SimpleNamespace(
        dialect=CLICKHOUSE_DIALECT,
        sql_context=ClickhouseSqlContext.from_context(CLICKHOUSE_DIALECT.sql_context).copy(
            cluster_name="books-cluster"
        ),
    )
    client.get_database_create_sql = partial(ClickhouseClient.get_database_create_sql, client)
    client.get_database_drop_sql = partial(ClickhouseClient.get_database_drop_sql, client)
    emitted = []
    failure = RuntimeError("drop failed")

    async def execute_script(sql):
        emitted.append(sql)
        if sql.startswith('DROP DATABASE IF EXISTS "second"'):
            raise failure

    client.execute_script = execute_script
    shadow = ClickhouseShadowDatabase(client, [])
    await shadow.create_shadow_database("first", "live_first", [])
    await shadow.create_shadow_database("second", "live_second", [])
    with pytest.raises(RuntimeError) as raised:
        await shadow.__aexit__(None, None, None)
    assert raised.value is failure
    assert emitted[-2:] == [
        'DROP DATABASE IF EXISTS "second" ON CLUSTER "books-cluster" SYNC',
        'DROP DATABASE IF EXISTS "first" ON CLUSTER "books-cluster" SYNC',
    ]


@pytest.mark.asyncio
@pytest.mark.requires("clickhouse-connect")
@pytest.mark.parametrize("engine", ["Atomic", "Replicated", "Shared"])
@pytest.mark.parametrize("schema", [None, 'other"database'])
async def test_schema_compilation_selects_storage_and_companion_before_rendering(engine, schema, monkeypatch):
    pytest.importorskip("clickhouse_connect")
    from unittest.mock import AsyncMock

    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.ddl.schema_objects.view import View
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.exceptions import ConfigurationError

    context = await Hare.init(
        {
            "connections": {"default": "clickhouse+clickhouse-connect://localhost/default?cluster=alpha"},
            "apps": {"placement": {"models": [__name__], "default_connection": "default"}},
        },
        connect=False,
    )
    try:
        context.apps.bind_base_queries()
        model = context.apps.get_model("placement.Book")
        model._meta.schema = schema
        connection = context.connections.get("default")
        with pytest.raises(ConfigurationError, match="needs the engine"):
            ClickhouseSchemaEditor(connection).table_creation.get_definition(model)
        editor = ClickhouseSchemaEditor(connection, collect_sql=True, database_engines={schema or "default": engine})
        prefix = '"other""database".' if schema else ""
        placement = ' ON CLUSTER "alpha"' if engine == "Atomic" else ""
        storage = prefix + '"book storage"' + placement
        logical = prefix + '"book"' + placement
        definition = editor.table_creation.get_definition(model)
        assert f"CREATE TABLE {storage} (" in definition.get_create_sql(editor.TABLE_CREATE_TEMPLATE)
        assert definition.qualified_name == prefix + '"book storage"'
        options = model._meta.get_table_options(connection.dialect)
        companion = options.get_companion_table_sqls(model, editor, replaces=False)[0]
        assert f"TABLE IF NOT EXISTS {logical} AS {prefix}" + '"book storage"' in companion
        comment = "'TTL, ON CLUSTER untouched'"
        sql = editor.get_column_change_sql(
            model, editor.ALTER_FIELD_TEMPLATE, changes=f'MODIFY COLUMN "rating" COMMENT {comment}'
        )
        assert sql.split(";\n") == [
            f'ALTER TABLE {storage} MODIFY COLUMN "rating" COMMENT {comment}',
            f'ALTER TABLE {logical} MODIFY COLUMN "rating" COMMENT {comment}',
        ]
        rewrite = editor.get_column_change_sql(
            model,
            editor.ALTER_FIELD_TEMPLATE,
            changes='MODIFY COLUMN "rating" Int64',
            storage_changes='MODIFY COLUMN "rating" Int64, UPDATE "rating" = "rating" WHERE 1',
        )
        assert ", UPDATE" in rewrite.split(";\n")[0]
        assert ", UPDATE" not in rewrite.split(";\n")[1]
        assert [step.sql for step in editor.get_delete_model_plan(model).steps] == [
            f"DROP TABLE {logical}",
            f"DROP TABLE {storage}",
        ]
        view = View('read"view', RawSQLTerm("SELECT 'ON CLUSTER leave literal'"))
        view_sql = editor.views.get_view_create_sqls(model, view)[0]
        assert prefix + '"read""view"' + placement + " AS" in view_sql
        assert "SELECT 'ON CLUSTER leave literal'" in view_sql
        assert editor.get_row_change_table_sql(model) == prefix + '"book storage" ON CLUSTER "alpha"'
        assert editor.get_row_recovery(model) is None
        assert editor.get_schema_recovery(model) is None
        field = model._meta.fields_map["rating"]
        not_null = editor.column_backfill.get_not_null_plan(
            model, field, '"rating"', "Int32", column_has_db_default=False
        )
        assert not_null.steps[0].sql == (f'ALTER TABLE {prefix}"book storage" ON CLUSTER "alpha" APPLY DELETED MASK')
        assert f'ALTER TABLE {storage} MODIFY COLUMN "rating"' in not_null.steps[1].sql
        assert f'ALTER TABLE {logical} MODIFY COLUMN "rating"' in not_null.steps[1].sql
        assert all(step.recovery is None for step in not_null.steps)
        assert editor.table_clearing_class.get_model_target_sql(editor, model) == storage
        execute_script = AsyncMock()
        monkeypatch.setattr(connection, "execute_script", execute_script)
        raw = """ALTER TABLE "book" COMMENT COLUMN "rating" 'ON CLUSTER literal' """
        await connection.execute_built_script(raw)
        execute_script.assert_awaited_once_with(raw)
    finally:
        await Hare.close_connections()
        context.__exit__(None, None, None)
