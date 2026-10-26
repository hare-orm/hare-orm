"""Field contract matrix, the pydantic column: the row of every field sample is read into the schema
``pydantic_model_creator()`` makes of its model - the schema and its JSON Schema are built, and the schema's
JSON reads back as an equal schema."""

import pytest

from hare.contrib.pydantic import pydantic_model_creator
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase

REQUIRES = ("pydantic",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [
        sample.get_parameter()
        for sample in FIELD_SAMPLES
        if sample.registered_live and set(sample.requires) <= {"encryption", "pydantic"}
    ],
)
async def test_the_row_of_a_field_is_read_into_a_pydantic_schema_and_back(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    models = await SampleDatabase.create(contract_database, sample)
    row_model = models[-1]
    await SampleDatabase.create_rows(row_model, sample)

    schema = pydantic_model_creator(row_model)
    assert schema.model_json_schema()["properties"]
    for row in await row_model.objects.order_by("id"):
        read = await schema.from_hare_orm(row)
        schema_json = read.model_dump_json()
        assert schema.model_validate_json(schema_json).model_dump_json() == schema_json
