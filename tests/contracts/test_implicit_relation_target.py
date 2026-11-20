"""A detected implicit-key change updates the physical foreign key on an empty source table."""

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations import Migration, State
from hare.migrations.operations import AlterField, CreateModel
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("relationship", ["foreign-key", "one-to-one"])
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_implicit_target_change_repoints_the_physical_constraint(contract_database, relationship, composite):
    relation_field = fields.ForeignKeyField if relationship == "foreign-key" else fields.OneToOneField
    connection = contract_database.get_connection()
    editor = connection.dialect.schema_editor_class(connection)
    initial = State(default_connections={CONTRACT_APP: CONTRACT_CONNECTION})
    child_fields = [
        ("id", fields.IntField(primary_key=not composite, generated=False)),
        ("parent", relation_field(f"{CONTRACT_APP}.Parent", to_field="code")),
    ]
    if composite:
        child_fields.append(("region", fields.IntField()))
    state = await Migration(
        "initial",
        CONTRACT_APP,
        operations=[
            CreateModel(
                "Parent",
                [("id", fields.IntField(primary_key=True, generated=False)), ("code", fields.IntField(unique=True))],
            ),
            CreateModel("Child", child_fields, {"primary_key_attribute": ("region", "id")} if composite else {}),
        ],
    ).apply(initial, schema_editor=editor)
    parent_apps = state.apps
    parent = parent_apps.get_model(f"{CONTRACT_APP}.Parent")
    await parent_apps.get_queryset(parent).create(id=1, code=100)
    requested = state.clone()
    AlterField("Child", "parent", relation_field(f"{CONTRACT_APP}.Parent")).state_forward(CONTRACT_APP, requested)
    operations = state.get_operations(requested, CONTRACT_APP).operations
    assert [(type(operation), operation.name) for operation in operations] == [(AlterField, "parent")]
    migrated = await Migration("change_target", CONTRACT_APP, operations=operations).apply(
        state.clone(), schema_editor=editor
    )
    child_apps = migrated.apps
    child = child_apps.get_model(f"{CONTRACT_APP}.Child")
    table = (await DatabaseCatalog.inspect_tables(connection, [migrated.apps.get_binding(child).db_table]))[0]
    assert table.foreign_keys["parent_id"].target_column == "id"
    await child_apps.get_queryset(child).create(id=1, parent_id=1, **({"region": 7} if composite else {}))
    assert (await child_apps.get_queryset(child).get(id=1)).parent_id == 1
    assert (await parent_apps.get_queryset(parent).get(id=1)).code == 100
