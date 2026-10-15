"""The pydantic schema of container fields - arrays, maps and tuples held in one another to any
depth - and of ClickHouse's values of several types: each annotated with the Python type of its
values, a document validated through them."""

import datetime
import decimal
import typing
import uuid

import pytest
from pydantic import ValidationError

from hare.contrib.pydantic import pydantic_model_creator
from tests.dialects.clickhouse.dynamic_types.models import Sample
from tests.dialects.clickhouse.models import Shipment


def test_containers_are_annotated_to_any_depth():
    annotations = {name: field.annotation for name, field in pydantic_model_creator(Shipment).model_fields.items()}
    assert annotations["tags"] == list[str]
    assert annotations["scores"] == list[int | None] | None
    assert annotations["grid"] == list[list[int]]
    assert annotations["prices"] == dict[str, decimal.Decimal]
    assert annotations["labels"] == dict[str, list[str]]
    assert annotations["bounds"] == tuple[int, datetime.datetime]
    assert annotations["deep"] == list[dict[str, list[tuple[int, dict[int, uuid.UUID | None]]]]]
    # A tuple of named elements is a dict of exactly them.
    assert typing.get_type_hints(annotations["point"]) == {"lon": float, "lat": float}
    assert typing.get_args(annotations["items"])[0].__required_keys__ == frozenset({"sku", "quantity"})


def test_a_document_is_validated_through_the_containers():
    schema = pydantic_model_creator(Shipment)
    key = uuid.uuid4()
    document = {
        "id": 1,
        "tags": ["a"],
        "scores": [1, None],
        "grid": [[1], []],
        "prices": {"eur": "1.50"},
        "labels": {"en": ["one"]},
        "point": {"lon": 1.5, "lat": 2.5},
        "bounds": [5, "2024-01-02T03:04:05Z"],
        "items": [{"sku": "a-1", "quantity": 2}],
        "deep": [{"k": [[1, {"10": str(key), "11": None}]]}],
    }
    shipment = schema.model_validate(document)
    assert shipment.prices == {"eur": decimal.Decimal("1.50")}
    assert shipment.bounds[1] == datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    assert shipment.deep == [{"k": [(1, {10: key, 11: None})]}]
    for broken in ({"grid": [[1, "x"]]}, {"point": {"lon": 1.5}}, {"deep": [{"k": [[1, {"10": "no uuid"}]]}]}):
        with pytest.raises(ValidationError):
            schema.model_validate({**document, **broken})


def test_values_of_several_types():
    annotations = {name: field.annotation for name, field in pydantic_model_creator(Sample).model_fields.items()}
    assert annotations["anything"] == typing.Any | None
    assert annotations["reading"] == int | str | list[float] | None
    assert annotations["bag"] == list[typing.Any | None]
