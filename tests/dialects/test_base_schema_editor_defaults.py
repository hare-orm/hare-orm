"""What the schema editor's and the introspector's parts do by default - for a dialect overriding none
of them; every dialect of hare overrides some, a new one starts from these."""

from types import SimpleNamespace

import pytest

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.dialects.base.schema.runtime_statements.table_locks import TableLocks
from hare.dialects.base.schema.runtime_statements.tenant_conditions import TenantConditions
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.base.schema.triggers.trigger_statements import TriggerStatements
from hare.dialects.sqlite.schema.relations.sqlite_foreign_key_rebuild import SqliteForeignKeyRebuild
from hare.fields import IntField
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.models import Model
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


class DefaultsItem(Model):
    id = IntField(primary_key=True)
    code = IntField()

    class Meta:
        app = "defaults"
        table = "defaults_item"


def get_editor() -> BaseSchemaEditor:
    return BaseSchemaEditor(FakeClient("sqlite"))


def test_the_default_sql_of_the_parts():
    editor = get_editor()
    limit = EnumValuesLimit(values=frozenset({"b", "a"}))
    assert (
        editor.column_narrowing_check.get_enum_overflow_predicate_sql(limit, '"mood"') == "\"mood\" NOT IN ('a', 'b')"
    )
    assert editor.index_statements.format_index_type("hash") == "hash "
    assert TableLocks.get_lock_table_sql('"defaults_item"') is None
    assert TenantConditions.get_set_column_count_sql(['"a"', '"b"']) == (
        '(CASE WHEN "a" IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN "b" IS NOT NULL THEN 1 ELSE 0 END)'
    )
    assert editor.constraint_statements.get_without_overlaps_extension(("code",), {"code": IntField()}) is None
    assert editor.table_partitions.get_partition_table_names(DefaultsItem) == []
    error = editor.views.get_unsupported_error("Views")
    assert str(error) == "Views are not supported on sqlite"


@pytest.mark.asyncio
async def test_the_default_statements_of_the_parts():
    editor = get_editor()
    client = editor.client
    await editor.constraint_statements.add_unique_constraint_using_index(
        DefaultsItem, UniqueConstraint(fields=("code",), name="defaults_item_code"), "defaults_item_code_index"
    )
    # The constraint's own index is built, the one it would take over dropped.
    assert client.executed[-1].startswith('DROP INDEX "defaults_item_code_index"')
    executed_count = len(client.executed)
    # A comment the table keeps inside its CREATE TABLE is changed with the table - nothing is run.
    await editor.table_comments.alter_table_comment(DefaultsItem)
    assert len(client.executed) == executed_count


@pytest.mark.asyncio
async def test_the_default_object_changes_go_through_the_other_parts(monkeypatch):
    editor = get_editor()
    calls: list[tuple[str, object]] = []

    async def drop_dictionary(self, model, dictionary):
        calls.append(("drop", dictionary))

    async def create_dictionary(self, model, dictionary):
        calls.append(("create", dictionary))

    monkeypatch.setattr(Dictionaries, "drop_dictionary", drop_dictionary)
    monkeypatch.setattr(Dictionaries, "create_dictionary", create_dictionary)
    old, new = SimpleNamespace(name="old"), SimpleNamespace(name="new")
    await editor.dictionaries.alter_dictionary(DefaultsItem, old, new)
    assert calls == [("drop", old), ("create", new)]

    monkeypatch.setattr(
        TriggerStatements, "get_trigger_create_sqls", lambda self, model, trigger: ["CREATE TRIGGER one", "SECOND"]
    )
    await editor.trigger_statements.add_trigger(DefaultsItem, SimpleNamespace(name="audit"))
    assert editor.client.executed[-2:] == ["CREATE TRIGGER one", "SECOND"]

    constraint = ForeignKeyConstraint(
        fields=("code",), to_table='"defaults_item"', to_fields=("id",), on_delete="CASCADE", name="fk_code"
    )
    await editor.foreign_key_rebuild.add_foreign_key_constraint_not_valid(DefaultsItem, constraint)
    assert editor.client.executed[-1].startswith('ALTER TABLE "defaults_item" ADD CONSTRAINT "fk_code" FOREIGN KEY')

    altered: list[object] = []

    async def alter_foreign_key_on_delete(self, model, column, old_field, new_field):
        altered.append((column, old_field is new_field))

    monkeypatch.setattr(ForeignKeyRebuild, "alter_foreign_key_on_delete", alter_foreign_key_on_delete)
    await editor.foreign_key_rebuild.rebuild_foreign_key_constraints(
        DefaultsItem, SimpleNamespace(source_fields=("owner_id",))
    )
    assert altered == [("owner_id", True)]


@pytest.mark.asyncio
async def test_sqlite_rebuilds_a_table_for_its_foreign_keys(monkeypatch):
    editor = get_editor()
    remade: list[tuple[type[Model], type[Model]]] = []

    async def remake_table(self, old_model, new_model):
        remade.append((old_model, new_model))

    monkeypatch.setattr(TableRebuild, "remake_table", remake_table)
    await SqliteForeignKeyRebuild(editor).rebuild_foreign_key_constraints(DefaultsItem, SimpleNamespace())
    assert remade == [(DefaultsItem, DefaultsItem)]


@pytest.mark.asyncio
async def test_the_default_answers_of_the_introspector(monkeypatch):
    connection = FakeClient("sqlite")
    assert SchemaIntrospector.parse_array_literal("{1,2}", "integer") is None
    assert SchemaIntrospector.maps_unbounded_char_to_text() is False
    assert await SchemaIntrospector.fetch_extensions(connection) == set()
    assert await SchemaIntrospector.fetch_views(connection, "main") == []

    async def fetch_default_schema(connection):
        return "main"

    async def fetch_table_names(connection, schema, include_partitions):
        return ["defaults_item"] if schema == "main" else []

    monkeypatch.setattr(SchemaIntrospector, "fetch_default_schema", fetch_default_schema)
    monkeypatch.setattr(SchemaIntrospector, "fetch_table_names", fetch_table_names)
    assert await SchemaIntrospector.fetch_table_exists(connection, "defaults_item") is True
    assert await SchemaIntrospector.fetch_table_exists(connection, "missing") is False
