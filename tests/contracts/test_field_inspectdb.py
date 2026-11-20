"""Field contract matrix, the inspectdb column: the table of every field sample's model, made by its migration,
is read by inspectdb, and the models it builds of the tables make the same tables - the column of each field is
as the sample declares it."""

import pytest

from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.declarations.declaration_values import DeclarationValues
from hare.inspectdb import ModelFactory
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.state.state import State
from tests.contracts.constants import CONTRACT_APP, FIELD_SAMPLES_WITHOUT_ROW_COLUMN
from tests.contracts.contract_models import ContractModels
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample.get_parameter() for sample in FIELD_SAMPLES if sample.name not in FIELD_SAMPLES_WITHOUT_ROW_COLUMN],
)
async def test_the_table_of_a_field_is_inspected_as_a_model_making_the_same_table(contract_database, sample):
    declaration_graph = ModelBindingGraph()
    SampleDatabase.skip_unless_column(sample)
    models = ContractModels.make_models(sample)
    connection = contract_database.get_connection()
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    migration, _ = SampleDatabase.get_written_migration(models)
    state_before = State()
    await migration.apply(state_before, schema_editor=editor)

    expected_catalog = {}
    inspected_models = []
    for model in models:
        table_info = await DatabaseCatalog.inspect_table(connection, declaration_graph.include(model).db_table)
        expected_catalog[table_info.name] = DeclarationValues.get(table_info)
        inspected_models.append(
            ModelFactory.from_table_info(table_info, connection.dialect.name, app_label=CONTRACT_APP)
        )

    # The database canonicalizes its own expressions and type aliases. Removing syntax
    # from SQL text would also erase meaningful quotes, grouping and literal contents.
    await migration.unapply(state_before, schema_editor=editor)
    remaining_tables = set(await DatabaseCatalog.get_table_names(connection))
    assert remaining_tables.isdisjoint(expected_catalog), remaining_tables & expected_catalog.keys()
    await SampleDatabase.apply_migration(contract_database, inspected_models)
    actual_catalog = {}
    for table_name in expected_catalog:
        table_info = await DatabaseCatalog.inspect_table(connection, table_name)
        actual_catalog[table_name] = DeclarationValues.get(table_info)
    assert actual_catalog == expected_catalog
