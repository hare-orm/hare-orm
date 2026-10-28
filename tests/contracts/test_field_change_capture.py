"""Field contract matrix, the change capture column: a row of every field sample, captured into an outbox, is
inserted, updated and deleted - each event is written, its payload is strict JSON (no NaN token) and holds the
value's JSON form; a field the capture leaves out (sensitive) isn't in it."""

import json

import pytest

from tests.contracts.contract_models import ContractModels
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.utils.database_under_test import DatabaseUnderTest


def refuse_constant(name: str) -> None:
    """json.loads()'s parse_constant - a NaN or infinity token isn't JSON."""
    raise ValueError(f"{name} is no JSON value")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample.get_parameter() for sample in FIELD_SAMPLES if sample.values and SampleDatabase.is_capturable(sample)],
)
async def test_a_captured_change_holds_the_json_form_of_the_value(contract_database, sample):
    connection = contract_database.get_connection()
    SampleDatabase.skip_unless_stored(sample)
    dialect = DatabaseUnderTest.get_dialect()
    if not dialect.features.supports_unique_constraints:
        pytest.skip(f"{dialect.name} keeps nothing unique - an outbox on it is refused when bound")
    outbox = ContractModels.make_outbox()
    models = await SampleDatabase.create(contract_database, sample, outbox)
    row_model = models[-1]
    numbers = range(1, len(sample.values) + 1)
    for number, value in zip(numbers, sample.values, strict=True):
        await row_model.objects.create(id=number, **{sample.column_name: value})
    for number, value in zip(numbers, sample.values, strict=True):
        await row_model.objects.filter(id=number).update(**{sample.column_name: value})
    await row_model.objects.all().delete()

    literals = connection.dialect.literals
    rows = await connection.execute_dicts(
        f"SELECT {literals.quote_identifier('payload')} AS payload FROM "
        f"{literals.quote_identifier(outbox._meta.db_table)} ORDER BY {literals.quote_identifier('sequence')}"
    )
    payloads = [
        json.loads(row["payload"], parse_constant=refuse_constant)
        if isinstance(row["payload"], str)
        else row["payload"]
        for row in rows
    ]
    held_by_operation = {"inserted": [], "updated": [], "deleted": []}
    for payload in payloads:
        values = payload["before"] if payload["operation"] == "deleted" else payload["after"]
        held_by_operation[payload["operation"]].append(values)
    expected = list(sample.json_values) if sample.captured else []
    for operation, held in held_by_operation.items():
        assert len(held) == len(sample.values), operation
        if sample.captured:
            assert [values[sample.column_name] for values in held] == expected, operation
        else:
            assert all(sample.column_name not in values for values in held), operation
