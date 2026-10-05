"""A filter's values of a field the ClickHouse dialect writes its own way (a UUID as the UUID itself,
a native JSON document) are still converted as the field's to_lookup_value() converts them - by the
native codec, where one is built."""

import uuid

import pytest

from hare import fields
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from tests.testmodels import UUIDFkRelatedModel

SAMPLES = {
    fields.UUIDField: [uuid.uuid4(), uuid.UUID(int=0), str(uuid.uuid4())],
    fields.JSONField: [{"a": [1, 2.5, "x"]}, [1, 2], "text", 5, {"nested": {"deep": True}}],
}


@pytest.mark.parametrize("stores_json_natively", [True, False])
@pytest.mark.parametrize("field_class", list(SAMPLES))
def test_a_lookup_value_is_the_field_s_own(stores_json_natively, field_class):
    if HydrateAccelerator.module is None:
        pytest.skip("the native module isn't built")
    types = ClickhouseDialect(stores_json_natively=stores_json_natively).types
    field = field_class(null=True)
    field.model = UUIDFkRelatedModel
    field.model_field_name = "value"
    codec = HydrateAccelerator.get_lookup_codec(field, types)
    assert codec is not None
    for value in SAMPLES[field_class]:
        expected = field.to_lookup_value(value, UUIDFkRelatedModel)
        written = codec[0].write(value, UUIDFkRelatedModel)
        assert written == expected
        assert type(written) is type(expected)


def test_a_uuid_list_is_written_as_text():
    dialect = ClickhouseDialect()
    field = UUIDFkRelatedModel._meta.fields_map["id"]
    values = [uuid.uuid4() for _ in range(3)]
    assert ValueEncoders.encode_list(values, UUIDFkRelatedModel, field, dialect) == [str(value) for value in values]
