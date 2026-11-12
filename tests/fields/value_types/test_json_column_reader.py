"""The reader of a JSONField column: the built-in field's values are decoded natively - the decoder its
own ``from_db_value()`` calls - and every other JSONField keeps its ``from_db_value()``."""

from typing import Any

import pytest

from hare.exceptions import ValidationError
from hare.fields import JSONField
from hare.fields.data.json.json_codec import JsonCodec
from hare.native.native_modules import NativeModules

pytestmark = pytest.mark.database_independent


def make_field(**options: Any) -> JSONField[Any]:
    field: JSONField[Any] = JSONField(**options)
    field.model_field_name = "payload"
    return field


def test_the_builtin_field_is_read_natively_as_its_from_db_value_reads():
    field = make_field()
    reader = field.get_db_value_reader()
    assert isinstance(reader, NativeModules.rows.JsonColumnReader)
    for raw in ('{"a":[1,2,{"b":null}]}', b'{"a":"\\u00e9"}', '"text"', "12", "[]"):
        assert reader(raw) == field.from_db_value(raw)


def test_a_value_the_driver_decoded_and_null_are_kept():
    reader = make_field().get_db_value_reader()
    document = {"a": 1}
    assert reader(document) is document
    assert reader(None) is None


def test_an_integer_past_64_bits_is_read_exactly():
    reader = make_field().get_db_value_reader()
    assert reader('{"big":18446744073709551616,"small":-9223372036854775809}') == {
        "big": 2**64,
        "small": -(2**63) - 1,
    }


@pytest.mark.parametrize("raw", ["{not json", '{"a":NaN}', "[1,", b"\xff"])
def test_text_that_isnt_json_raises_the_fields_own_error(raw):
    field = make_field()
    with pytest.raises(ValidationError) as from_field:
        field.from_db_value(raw)
    with pytest.raises(ValidationError) as from_reader:
        field.get_db_value_reader()(raw)
    assert str(from_reader.value) == str(from_field.value)


def test_a_sensitive_fields_text_is_hidden_in_the_error():
    with pytest.raises(ValidationError) as error:
        make_field(sensitive=True).get_db_value_reader()('{"secret": oops}')
    assert "oops" not in str(error.value)


def test_a_field_reading_its_own_way_keeps_from_db_value():
    def decode(text: str | bytes) -> Any:
        return JsonCodec.loads(text)

    class UpperJSONField(JSONField[Any]):
        def from_db_value(self, value: Any) -> Any:
            return super().from_db_value(value.upper() if isinstance(value, str) else value)

    custom_decoder = make_field(decoder=decode)
    assert custom_decoder.get_db_value_reader() == custom_decoder.from_db_value
    overriding = UpperJSONField()
    overriding.model_field_name = "payload"
    assert overriding.get_db_value_reader() == overriding.from_db_value
    assert overriding.get_db_value_reader()('"abc"') == "ABC"


def test_json_codec_loads_is_the_native_decoder():
    assert JsonCodec.loads is NativeModules.rows.loads_json
    assert JsonCodec.loads('{"a":[1,2.5,"x",true,null]}') == {"a": [1, 2.5, "x", True, None]}
    assert JsonCodec.loads(b"[18446744073709551616]") == [2**64]
    with pytest.raises(ValueError):
        JsonCodec.loads("NaN")
