"""JsonValues - the JSON form of a Python value, every value inside a container too: what a field's
get_json_value() writes by default, an outbox event's values among them."""

import datetime
import decimal
import enum
import uuid

import pytest

from hare import fields
from hare.fields.json_values import JsonValues

pytestmark = pytest.mark.database_independent


class Color(enum.Enum):
    RED = "red"


@pytest.mark.parametrize(
    ("value", "json_value"),
    [
        pytest.param(None, None, id="none"),
        pytest.param(1.5, 1.5, id="float"),
        pytest.param(float("nan"), "NaN", id="nan"),
        pytest.param(float("-inf"), "-Infinity", id="negative_infinity"),
        pytest.param((1, "a"), [1, "a"], id="tuple"),
        pytest.param(
            {"when": datetime.date(2026, 1, 2), "items": [decimal.Decimal("1.50"), float("inf")]},
            {"when": "2026-01-02", "items": ["1.50", "Infinity"]},
            id="nested",
        ),
        pytest.param({Color.RED: 1}, {"red": 1}, id="enum_key"),
        pytest.param(uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001", id="uuid"),
    ],
)
def test_a_value_has_its_json_form(value, json_value):
    assert JsonValues.get_json_value(value) == json_value


@pytest.mark.parametrize(
    "value",
    [pytest.param({1: "a"}, id="number_key"), pytest.param({1, 2}, id="set"), pytest.param(object(), id="object")],
)
def test_a_value_without_a_json_form_raises(value):
    with pytest.raises(TypeError):
        JsonValues.get_json_value(value)


@pytest.mark.parametrize(
    "element_fields",
    [
        [fields.FloatField(), fields.UUIDField()],
        {"ratio": fields.FloatField(), "key": fields.UUIDField()},
    ],
    ids=["positional", "named"],
)
def test_a_tuple_has_each_element_in_the_json_form_of_its_field(element_fields):
    key = uuid.UUID(int=1)

    assert fields.TupleField(element_fields).get_json_value((float("nan"), key)) == ["NaN", str(key)]


def test_a_decimal_has_the_json_form_of_the_value_its_column_holds():
    field = fields.DecimalField(max_digits=6, decimal_places=2)

    assert field.get_json_value(decimal.Decimal("1.5")) == "1.50"
    assert field.get_json_value(decimal.Decimal("1.005")) == "1.01"
    assert fields.ArrayField(field).get_json_value([decimal.Decimal("2")]) == ["2.00"]
    assert field.get_json_value(None) is None
