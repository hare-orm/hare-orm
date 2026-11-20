"""A filter's values of a field the ClickHouse dialect writes its own way (a UUID as the UUID itself,
a native JSON document) retain the field's lookup conversion."""

import uuid

import pytest

from hare import fields
from hare.core.hare_context import HareContext
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.exceptions import ValidationError
from hare.query.filters.lookups.value_encoders import ValueEncoders
from tests.shared_models.testmodels import UUIDFkRelatedModel

pytestmark = pytest.mark.database_independent

SAMPLES = {
    fields.UUIDField: [uuid.uuid4(), uuid.UUID(int=0), str(uuid.uuid4())],
    fields.JSONField: [{"a": [1, 2.5, "x"]}, {"nested": {"deep": True}}],
}


@pytest.mark.parametrize("stores_json_natively", [True, False])
@pytest.mark.parametrize("field_class", list(SAMPLES))
def test_lookup_and_storage_share_the_registered_column_conversion(stores_json_natively, field_class):
    types = ClickhouseDialect(stores_json_natively=stores_json_natively).types
    field = field_class(null=True)
    field.model = UUIDFkRelatedModel
    field.model_field_name = "value"
    for value in SAMPLES[field_class]:
        expected = types.get_db_value(field, value, UUIDFkRelatedModel)
        written = types.get_lookup_value(field, value, UUIDFkRelatedModel)
        prepared = types.get_lookup_writer(field)(value, UUIDFkRelatedModel)
        assert written == prepared == expected
        assert type(written) is type(expected)


@pytest.mark.parametrize("value", [[1, 2], "text", 5])
def test_native_json_lookup_refuses_values_the_column_cannot_store(value):
    types = ClickhouseDialect(stores_json_natively=True).types
    field = fields.JSONField(null=True)
    field.model_field_name = "value"
    with pytest.raises(ValidationError, match="holds an object"):
        types.get_lookup_value(field, value, UUIDFkRelatedModel)


@pytest.mark.usefixtures("model_conversion_context")
def test_uuid_list_retains_the_registered_uuid_representation():
    dialect = ClickhouseDialect()
    field = HareContext.require_current().apps.get_binding(UUIDFkRelatedModel).fields_map["id"]
    values = [uuid.uuid4() for _ in range(3)]
    assert ValueEncoders.encode_list(values, UUIDFkRelatedModel, field, dialect) == values
