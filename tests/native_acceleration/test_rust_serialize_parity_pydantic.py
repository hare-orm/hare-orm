"""The native write path of a JSONField whose field_type is a pydantic model: it never takes the native JSON
codec, whose value check lacks the model's shape check."""

import pytest

from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ValidationError
from hare.query.rows.enums import WriteCodecType
from hare.query.rows.native.field_codecs import FieldCodecs
from hare.time import Timezone
from tests.shared_models.testmodels_pydantic import PydanticJSONFields

REQUIRES = ("pydantic",)


@pytest.mark.database_independent
def test_pydantic_typed_json_field_falls_back_to_complex_bucket():
    """A field_type=SomePydanticModel JSONField (data_pydantic) must NOT take bucket 6 - the
    Rust-side json_field_value() has no equivalent to to_db_value()'s own field_type(**value)
    shape check, so bucket 6 would silently skip it (see the fix's own comment in
    rust_serialize_mixin.py for the full reasoning)."""
    field = PydanticJSONFields._meta.fields_map["data_pydantic"]
    codec_type = FieldCodecs.get_write_specification(field, SQL_DIALECT.types, Timezone.get_aware_zone_name())[0]
    assert codec_type == WriteCodecType.CALL


@pytest.mark.asyncio
async def test_pydantic_typed_json_field_rejects_malformed_dict_via_bulk_create(db):
    """End-to-end sibling of test_encrypted_style_json_subclass_override_actually_runs_on_real_write
    below: the real bug this fixes was bulk_create() (the only write path that goes through
    _build_rust_serialize_plan/hydrate.serialize_rows at all) silently writing a dict that
    doesn't match data_pydantic's own TestSchemaForJSONField shape, instead of raising
    ValidationError the way .save()/create() (which never go through the Rust bucket
    classification) already did."""
    malformed = {"totally": "wrong shape"}

    with pytest.raises(ValidationError):
        await PydanticJSONFields.objects.create(data={"a": 1}, data_pydantic=malformed)

    with pytest.raises(ValidationError):
        await PydanticJSONFields.objects.bulk_create([PydanticJSONFields(data={"a": 1}, data_pydantic=malformed)])
