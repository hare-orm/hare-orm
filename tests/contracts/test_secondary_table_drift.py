"""A missing secondary table cannot hide behind the model's surviving storage table."""

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.ddl.enums import SchemaPartType
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.drift.shadow.schema_comparison import SchemaComparison
from hare.migrations.state.state import State
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_missing_secondary_table_reports_drift_without_recreating_storage(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        title = fields.TextField()

    class BookProjection(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        title = fields.TextField()

    contract_database.register_live_models([Book, BookProjection], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, title="preserved")
    connection = contract_database.get_connection()
    editor = connection.dialect.schema_editor_class(connection)
    state = State.from_models([Book], default_connections={CONTRACT_APP: CONTRACT_CONNECTION})
    key = (CONTRACT_APP, "Book")
    storage, secondary = (
        HareContext.require_current().apps.get_binding(Book).db_table,
        HareContext.require_current().apps.get_binding(BookProjection).db_table,
    )
    introspector = DatabaseCatalog.get_introspector_class(connection)
    schema = await DatabaseCatalog.get_default_schema(connection)
    expected_parts = await introspector.fetch_schema_parts(connection, schema, [storage, secondary], {})
    baseline = SchemaComparison(state, editor, expected_parts, expected_parts)
    baseline.compare_model(key, state.models[key], [storage, secondary])
    assert baseline.operations == []
    assert baseline.mismatched_objects == []

    await editor.delete_model(HareContext.require_current().apps.get_binding(BookProjection))
    actual_parts = await introspector.fetch_schema_parts(connection, schema, [storage], {})
    comparison = SchemaComparison(state, editor, actual_parts, expected_parts)
    comparison.compare_model(key, state.models[key], [storage, secondary])
    assert comparison.operations == []
    assert [(part.part_type, part.table, part.name) for part in comparison.mismatched_objects] == [
        (SchemaPartType.TABLE, secondary, secondary)
    ]
    assert (await Book.objects.get(number=1)).title == "preserved"
