"""Generated columns preserve their output field's values, JSON form and query behavior."""

import pytest

from tests.contracts.constants import GENERATED_FIELD_OPERATIONS, GENERATED_VALUE_FIELD_NAME, SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.generated_field_contract import GeneratedFieldContract
from tests.contracts.generated_field_expectations import GeneratedFieldExpectations
from tests.contracts.generated_field_samples import (
    GENERATED_JSON_OBJECT_VALUES,
    GENERATED_OPERATION_SAMPLES,
    GENERATED_VALUE_OPERATIONS,
)
from tests.contracts.sample_database import SampleDatabase


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample.get_parameter() for sample in FIELD_SAMPLES if sample.values and not sample.related],
)
async def test_generated_columns_preserve_the_output_field_contract(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    models = await SampleDatabase.create(contract_database, GeneratedFieldContract.with_generated_copy(sample))
    model = models[-1]
    await SampleDatabase.create_rows(model, sample)
    contract = GeneratedFieldContract(model, sample)
    dialect = contract_database.get_connection().dialect.name
    for operation in GeneratedFieldExpectations.get_operations(sample):
        original = await contract.get_outcome(operation, SAMPLED_FIELD_NAME, dialect)
        generated = await contract.get_outcome(operation, GENERATED_VALUE_FIELD_NAME, dialect)
        assert original == generated, operation
        if original[0] == "error":
            continue
        if operation in GENERATED_VALUE_OPERATIONS:
            assert original == ("value", contract.canonical(GeneratedFieldExpectations.get_json_values(sample)))
        if operation == "json_object":
            assert original == (
                "value",
                contract.canonical(GeneratedFieldExpectations.get_json_objects(sample, dialect)),
            )
        if operation == "coalesce_fractional":
            assert original == (
                "value",
                contract.canonical([0.5 if value is None else float(value) for value in sample.values]),
            )
        if operation in {"ordering", "annotation_ordering"}:
            assert original == (
                "value",
                contract.canonical(GeneratedFieldExpectations.get_ordered_ids(sample, dialect)),
            )
        if operation == "filter":
            expected = [
                [number for number, candidate in enumerate(sample.values, 1) if candidate == value]
                for value in sample.values
            ]
            assert original == ("value", contract.canonical(expected))
        if operation == "distinct":
            expected = sorted(
                {contract.canonical(value) for value in GeneratedFieldExpectations.get_json_values(sample)}
            )
            assert original == ("value", contract.canonical(expected))
        if operation == "aggregate":
            assert original == ("value", contract.canonical(GeneratedFieldExpectations.get_extrema(sample)))
        if operation == "aggregate_empty":
            assert original == ("value", contract.canonical({"minimum": None, "maximum": None}))
        if operation == "greatest_least":
            expected = [
                {"minimum": value, "maximum": value} for value in GeneratedFieldExpectations.get_json_values(sample)
            ]
            assert original == ("value", contract.canonical(expected))
        if operation == "lag_fractional":
            expected = [0.5, *GeneratedFieldExpectations.get_json_values(sample)[:-1]]
            assert original == ("value", contract.canonical(expected))


@pytest.mark.database_independent
def test_generated_operation_inventory_names_existing_samples_and_covers_every_operation():
    sample_names = {sample.name for sample in FIELD_SAMPLES if sample.values and not sample.related}
    assert GENERATED_VALUE_OPERATIONS | GENERATED_OPERATION_SAMPLES.keys() == set(GENERATED_FIELD_OPERATIONS)
    assert GENERATED_JSON_OBJECT_VALUES.keys() == GENERATED_OPERATION_SAMPLES["json_object"]
    for operation, names in GENERATED_OPERATION_SAMPLES.items():
        assert names and names <= sample_names, (operation, names - sample_names)
