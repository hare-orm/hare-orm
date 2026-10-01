"""Parameter types read from one text value, and the check of an annotation against a filter's value."""

import datetime
from enum import Enum, IntEnum, StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from hare.contrib.request_query import CommaSeparated, KeyColumns, TextParameter
from hare.contrib.request_query.value_annotations import ValueAnnotation
from hare.query.enums import LookupValueShape


class Color(StrEnum):
    RED = "red"


class Level(IntEnum):
    LOW = 1


class Mixed(Enum):
    ONE = 1
    TWO = "two"


def test_a_composite_key_from_text():
    adapter = TypeAdapter(KeyColumns[int, str])
    assert adapter.validate_python("2,a%2Cb") == (2, "a,b")
    assert adapter.validate_python([3, "x"]) == (3, "x")
    with pytest.raises(ValidationError):
        adapter.validate_python("2")
    uuid_key = TypeAdapter(KeyColumns[UUID, int]).validate_python("7b2e8a3c-6f3e-4f8a-9a55-1a2b3c4d5e6f,4")
    assert uuid_key == (UUID("7b2e8a3c-6f3e-4f8a-9a55-1a2b3c4d5e6f"), 4)


def test_a_comma_separated_list():
    adapter = TypeAdapter(CommaSeparated[int])
    assert adapter.validate_python("1, 2,,3") == [1, 2, 3]
    assert adapter.validate_python(["1,2", "3"]) == [1, 2, 3]
    assert adapter.validate_python([4, 5]) == [4, 5]
    with pytest.raises(ValidationError):
        adapter.validate_python("1,x")


def test_text_types_are_marked_for_framework_adapters():
    for annotation in (KeyColumns[int, int], CommaSeparated[int]):
        assert any(isinstance(item, TextParameter) for item in annotation.__metadata__)
    assert TypeAdapter(CommaSeparated[int]).json_schema()["type"] == "string"


@pytest.mark.parametrize(
    ("annotation", "expected_type", "accepted"),
    [
        (int, int, True),
        (int | None, int, True),
        (Annotated[int, "meta"] | None, int, True),
        (bool, int, False),
        (bool, bool, True),
        (str, int, False),
        (datetime.datetime, datetime.date, True),
        (datetime.date, datetime.datetime, False),
        (Color, str, True),
        (Level, int, True),
        (Mixed, int, False),
        (Literal["a", "b"], str, True),
        (Literal[1, True], int, False),
        (int | str, int, False),
        (Any, int, False),
        (Any, object, True),
        (tuple[int, str], (int, str), True),
        (tuple[int, int], (int, str), False),
        (tuple[int, ...], (int, int), False),
        (int, (int, int), False),
    ],
)
def test_a_single_value(annotation, expected_type, accepted):
    assert ValueAnnotation.accepts_shape(annotation, LookupValueShape.VALUE, expected_type) is accepted


@pytest.mark.parametrize(
    ("annotation", "accepted"),
    [
        (list[int], True),
        (list[int] | None, True),
        (set[int], True),
        (frozenset[int], True),
        (tuple[int, ...], True),
        (tuple[int, int], False),
        (list[str], False),
        (int, False),
        (list, False),
    ],
)
def test_a_list(annotation, accepted):
    assert ValueAnnotation.accepts_shape(annotation, LookupValueShape.LIST, int) is accepted


@pytest.mark.parametrize(
    ("annotation", "accepted"),
    [
        (tuple[int, int], True),
        (list[int], True),
        (tuple[int, str], False),
        (tuple[int, ...], False),
        (int, False),
    ],
)
def test_a_range(annotation, accepted):
    assert ValueAnnotation.accepts_shape(annotation, LookupValueShape.RANGE, int) is accepted


def test_the_description_of_a_value():
    assert ValueAnnotation.describe(LookupValueShape.VALUE, int) == "int"
    assert ValueAnnotation.describe(LookupValueShape.LIST, UUID) == "list[UUID]"
    assert ValueAnnotation.describe(LookupValueShape.RANGE, datetime.date) == "tuple[date, date]"
    assert ValueAnnotation.describe(LookupValueShape.VALUE, (UUID, int)) == "tuple[UUID, int]"
    assert ValueAnnotation.describe(LookupValueShape.VALUE, object) == "any value"
