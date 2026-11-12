"""Field policy is applied before the selected encoder sees a document."""

import json

import pytest

from hare.exceptions import ValidationError
from hare.fields import JSONField

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_invalid_number_never_reaches_encoder(number):
    calls = []
    field = JSONField(encoder=lambda value: calls.append(value) or "null")
    with pytest.raises(ValidationError, match="finite"):
        field.to_db_value({"number": number}, None)
    assert calls == []


def test_long_integer_is_exact_after_preparation():
    value = {"integer": 10**100 + 1}
    assert json.loads(JSONField().to_db_value(value, None)) == value


def test_custom_encoder_receives_original_plain_value_once():
    calls = []
    value = {"message": "hello"}
    field = JSONField(encoder=lambda document: calls.append(document) or "encoded")
    assert field.to_db_value(value, None) == "encoded"
    assert len(calls) == 1
    assert calls[0] is value


def test_circular_value_never_reaches_encoder():
    value = []
    value.append(value)
    calls = []
    field = JSONField(encoder=lambda document: calls.append(document) or "encoded")
    with pytest.raises(ValidationError, match="[Cc]ircular"):
        field.to_db_value(value, None)
    assert calls == []
