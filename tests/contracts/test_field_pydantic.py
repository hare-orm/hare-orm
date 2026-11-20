"""Field contract matrix, the pydantic column: the row of every field sample is read into the schema
``pydantic_model_creator()`` makes of its model - the schema and its JSON Schema are built, and the schema's
JSON reads back as an equal schema."""

from functools import partial

import pytest
from pydantic import ValidationError as PydanticValidationError

from hare import Model, fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.core.hare_context import HareContext
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase


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


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
async def test_create_schema_leaves_pending_async_defaults_to_the_model(contract_database, composite):
    calls = []

    async def default_value(value=41):
        calls.append(value)
        return value

    class AsyncDefault:
        async def __call__(self):
            return await default_value(42)

    class DefaultRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.TextField()
            pk = fields.CompositePrimaryKey("region", "number")
        function_value = fields.IntField(default=default_value, source_field="function_db")
        object_value = fields.IntField(default=AsyncDefault(), source_field="object_db")
        partial_value = fields.IntField(default=partial(default_value, 43), source_field="partial_db")
        sync_value = fields.IntField(default=lambda: 17)
        nullable_value = fields.IntField(default=default_value, null=True)

    contract_database.register_live_models([DefaultRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    schema = pydantic_model_creator(DefaultRecord, exclude_readonly=True)
    keys = {"number": 1, **({"region": "r"} if composite else {})}
    payload = schema(**keys)
    assert payload.function_value is payload.object_value is payload.partial_value is None
    assert calls == []
    created = await DefaultRecord.objects.create(**payload.model_dump())
    assert (created.function_value, created.object_value, created.partial_value, created.sync_value) == (
        41,
        42,
        43,
        17,
    )
    assert created.nullable_value is None
    assert sorted(calls) == [41, 42, 43]
    assert payload.model_dump() == {**keys, "sync_value": 17, "nullable_value": None}
    with pytest.raises(PydanticValidationError):
        schema(**keys, function_value=None)
    optional_schema = pydantic_model_creator(DefaultRecord, exclude_readonly=True, optional=("function_value",))
    assert optional_schema(**keys, function_value=None).model_dump()["function_value"] is None
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    assert await connection.execute_dicts(
        f"SELECT function_db,object_db,partial_db,sync_value,nullable_value FROM "
        f'"{HareContext.require_current().apps.get_binding(DefaultRecord).db_table}"'
    ) == [{"function_db": 41, "object_db": 42, "partial_db": 43, "sync_value": 17, "nullable_value": None}]
