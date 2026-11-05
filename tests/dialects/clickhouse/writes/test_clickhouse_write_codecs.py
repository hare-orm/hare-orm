"""ClickHouse UUID writers preserve UUID objects and field validators."""

import uuid

import pytest

from hare import fields
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from tests.shared_models.testmodels import UUIDFkRelatedModel

pytestmark = pytest.mark.database_independent


class DriverUUID(uuid.UUID):
    pass


def reject_nil(value):
    if value == uuid.UUID(int=0):
        raise ValueError("nil")


@pytest.mark.parametrize("null", [True, False])
@pytest.mark.parametrize("validators", [None, [reject_nil]])
def test_a_written_uuid_is_the_uuid_itself(null, validators):
    types = ClickhouseDialect().types
    field = fields.UUIDField(null=null, validators=validators)
    field.model = UUIDFkRelatedModel
    field.model_field_name = "value"
    writer = types.get_db_writer(field)
    value = uuid.uuid4()
    for sample in (value, str(value), DriverUUID(int=value.int), None):
        written = writer(sample, UUIDFkRelatedModel)
        if sample is None:
            assert written is None
        else:
            assert written == value
            assert isinstance(written, uuid.UUID)
    assert writer(value, UUIDFkRelatedModel) is value
    if validators:
        with pytest.raises(Exception, match="nil"):
            writer(uuid.UUID(int=0), UUIDFkRelatedModel)
