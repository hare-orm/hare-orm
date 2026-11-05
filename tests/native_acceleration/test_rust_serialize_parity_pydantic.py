"""Single and bulk writes enforce a declared JSON schema."""

import pytest

from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ValidationError
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from tests.shared_models.testmodels_pydantic import PydanticJSONFields


@pytest.mark.database_independent
def test_batch_serializer_checks_typed_json_shape():
    obj = PydanticJSONFields(data={"a": 1}, data_pydantic={"foo": 1, "bar": "valid"})
    obj.data_pydantic = {"totally": "wrong shape"}
    with pytest.raises(ValidationError):
        BulkWriteBatches.serialize_instances(PydanticJSONFields, SQL_DIALECT.types, [obj], ["data_pydantic"])


@pytest.mark.asyncio
async def test_pydantic_typed_json_field_rejects_malformed_dict_via_bulk_create(db):
    malformed = {"totally": "wrong shape"}
    with pytest.raises(ValidationError):
        await PydanticJSONFields.objects.create(data={"a": 1}, data_pydantic=malformed)
    with pytest.raises(ValidationError):
        await PydanticJSONFields.objects.bulk_create([PydanticJSONFields(data={"a": 1}, data_pydantic=malformed)])
