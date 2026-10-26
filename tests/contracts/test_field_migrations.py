"""Field contract matrix, the migration column: every field sample's model is written into a migration, the
migration read back from its source, and the state it builds is the model's - a makemigrations after it finds
no change. On the database under test, where the field has a column, the migration is applied and reversed."""

import pytest

from hare.migrations.state.state import State
from tests.contracts.constants import CONTRACT_APP, CONTRACT_DIALECTS, SAMPLED_FIELD_NAME
from tests.contracts.contract_models import ContractModels
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [sample.get_parameter() for sample in FIELD_SAMPLES])
async def test_a_written_migration_of_a_field_reads_back_as_the_model(sample):
    migration, new_state = SampleDatabase.get_written_migration(ContractModels.make_models(sample))
    state = await migration.apply(State(), dry_run=True)
    assert state.get_operations(new_state, CONTRACT_APP).operations == ()


@pytest.mark.database_independent
@pytest.mark.parametrize("sample", [sample.get_parameter() for sample in FIELD_SAMPLES])
def test_a_field_has_a_column_on_some_dialect(sample):
    assert any(SampleDatabase.has_column(sample, dialect_name) for dialect_name in CONTRACT_DIALECTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [sample.get_parameter() for sample in FIELD_SAMPLES])
async def test_a_migration_of_a_field_is_applied_and_reversed_on_the_database(contract_database, sample):
    connection = contract_database.get_connection()
    dialect_name = DatabaseUnderTest.get_dialect().name
    field = sample.make_fields()[SAMPLED_FIELD_NAME]
    if not SampleDatabase.has_column(sample, dialect_name):
        pytest.skip(f"the field has no column on {dialect_name}")
    if getattr(field, "unique", False) and not DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
        pytest.skip(f"{dialect_name} keeps nothing unique - a model of the field is refused when bound")
    migration, _ = SampleDatabase.get_written_migration(ContractModels.make_models(sample))
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = await migration.apply(State(), schema_editor=editor)
    await migration.unapply(state, schema_editor=editor)
