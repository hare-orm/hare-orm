"""A value of a field the ClickHouse dialect writes its own way is written by the native codec as the
dialect's conversion writes it - a UUID as the UUID itself."""

import uuid

import pytest

from hare import fields
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.query.rows.enums import ReadCodecType
from hare.query.rows.native.field_codecs import FieldCodecs
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from tests.testmodels import UUIDFkRelatedModel


class DriverUUID(uuid.UUID):
    pass


def reject_nil(value):
    if value == uuid.UUID(int=0):
        raise ValueError("nil")


@pytest.mark.parametrize("null", [True, False])
@pytest.mark.parametrize("validators", [None, [reject_nil]])
def test_a_written_uuid_is_the_uuid_itself(null, validators):
    if HydrateAccelerator.module is None:
        pytest.skip("the native module isn't built")
    types = ClickhouseDialect().types
    field = fields.UUIDField(null=null, validators=validators)
    field.model = UUIDFkRelatedModel
    field.model_field_name = "value"
    codec_type, options = FieldCodecs.get_write_specification(field, types, None)
    codec = HydrateAccelerator.module.FieldCodec("value", ReadCodecType.AS_IS, {}, codec_type, options)
    writer = types.get_db_writer(field)
    value = uuid.uuid4()
    for sample in (value, str(value), DriverUUID(int=value.int), None):
        expected = writer(sample, UUIDFkRelatedModel)
        written = codec.write(sample, UUIDFkRelatedModel)
        assert written == expected
        assert type(written) is type(expected)
    assert codec.write(value, UUIDFkRelatedModel) is value
    if validators:
        for write in (writer, codec.write):
            with pytest.raises(Exception, match="nil"):
                write(uuid.UUID(int=0), UUIDFkRelatedModel)
