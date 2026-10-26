"""Field contract matrix, the inspectdb column: the table of every field sample's model, made by its migration,
is read by inspectdb, and the models it builds of the tables make the same tables - the column of each field is
as the sample declares it."""

import re

import pytest

from hare.inspectdb import ModelFactory
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from tests.contracts.constants import (
    CLICKHOUSE_STRING_PATTERN,
    CONTRACT_APP,
    FIELD_SAMPLES_WITH_DATABASE_EXPRESSION,
    FIELD_SAMPLES_WITHOUT_ROW_COLUMN,
)
from tests.contracts.contract_models import ContractModels
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample.get_parameter() for sample in FIELD_SAMPLES if sample.name not in FIELD_SAMPLES_WITHOUT_ROW_COLUMN],
)
async def test_the_table_of_a_field_is_inspected_as_a_model_making_the_same_table(contract_database, sample):
    SampleDatabase.skip_unless_column(sample)
    models = ContractModels.make_models(sample)
    await SampleDatabase.apply_migration(contract_database, models)

    connection = contract_database.get_connection()
    inspected_models = []
    for model in models:
        table_info = await DatabaseCatalog.inspect_table(connection, model._meta.db_table)
        inspected_models.append(
            ModelFactory.from_table_info(table_info, connection.dialect.name, app_label=CONTRACT_APP)
        )
    inspected_sql = sorted(await SampleDatabase.get_create_sql(contract_database, inspected_models))
    sampled_sql = sorted(await SampleDatabase.get_create_sql(contract_database, models))
    assert len(inspected_sql) == len(sampled_sql)
    for inspected_statement, sampled_statement in zip(inspected_sql, sampled_sql, strict=True):
        if sample.name in FIELD_SAMPLES_WITH_DATABASE_EXPRESSION:
            inspected_statement = re.sub(r"[\"()\s]", "", inspected_statement)
            sampled_statement = re.sub(r"[\"()\s]", "", sampled_statement)
        if DatabaseUnderTest.get_dialect().name == "clickhouse":
            inspected_statement = CLICKHOUSE_STRING_PATTERN.sub("String", inspected_statement)
            sampled_statement = CLICKHOUSE_STRING_PATTERN.sub("String", sampled_statement)
        assert inspected_statement == sampled_statement
