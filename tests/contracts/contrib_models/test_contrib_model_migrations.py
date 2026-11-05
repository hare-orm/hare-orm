"""Contrib model matrix, the migration column: the models of every case are written into a migration, the
migration read back from its source builds the models' state - a makemigrations after it finds no change - it
is applied on the database under test and reversed; and every model hare ships to build on has a case."""

import pytest

from hare.migrations.state.state import State
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.contrib_models.contrib_model_cases import CONTRIB_MODEL_CASES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest

PARAMETERS = [case.get_parameter() for case in CONTRIB_MODEL_CASES]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("case", PARAMETERS)
async def test_a_written_migration_of_the_models_reads_back_as_the_models(case):
    migration, new_state = SampleDatabase.get_written_migration(case.make_models())
    state = await migration.apply(State(), dry_run=True)
    assert state.get_operations(new_state, CONTRACT_APP).operations == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", PARAMETERS)
async def test_a_migration_of_the_models_is_applied_and_reversed_on_the_database(contract_database, case):
    models = case.make_models()
    dialect_name = DatabaseUnderTest.get_dialect().name
    if dialect_name not in case.dialects:
        pytest.skip(f"{case.name} is not supported on {dialect_name}")
    migration, _ = SampleDatabase.get_written_migration(models)
    connection = contract_database.get_connection()
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = await migration.apply(State(), schema_editor=editor)
    await migration.unapply(state, schema_editor=editor)


@pytest.mark.database_independent
def test_every_model_hare_ships_is_a_base_of_a_case():
    shipped = SampleDatabase.get_shipped_model_bases()
    covered = {
        base
        for case in CONTRIB_MODEL_CASES
        for model in case.make_models()
        for base in model.__mro__
        if isinstance(base, type) and issubclass(base, Model)
    }
    assert sorted(f"{base.__module__}.{base.__qualname__}" for base in shipped - covered) == []
