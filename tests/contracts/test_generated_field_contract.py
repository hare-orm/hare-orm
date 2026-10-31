"""Generated columns preserve their output field's values, JSON form and query behavior."""

import pytest

from hare.exceptions import QueryError
from tests.contracts.constants import GENERATED_FIELD_OPERATIONS, GENERATED_VALUE_FIELD_NAME, SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.generated_field_contract import GeneratedFieldContract
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
    failures = []
    ordering_outcomes = None
    for operation in GENERATED_FIELD_OPERATIONS:
        original, original_error = await contract.get_outcome(operation, SAMPLED_FIELD_NAME)
        generated, generated_error = await contract.get_outcome(operation, GENERATED_VALUE_FIELD_NAME)
        if operation == "json":
            assert original[0] == "value", original_error
            if sample.json_values:
                assert original[1] == contract.canonical(list(sample.json_values))
        if operation == "coalesce_fractional" and sample.name == "int":
            assert original == (
                "value",
                contract.canonical([0.5 if value is None else float(value) for value in sample.values]),
            )
        if operation == "lag_fractional" and sample.name == "int":
            assert original == ("error", QueryError)
        if operation == "ordering":
            ordering_outcomes = (original, generated)
        if operation == "annotation_ordering" and ordering_outcomes != (original, generated):
            failures.append((operation, "differs from direct ordering", original, generated))
        if original != generated:
            failures.append((operation, original, generated, original_error, generated_error))
    assert failures == []
