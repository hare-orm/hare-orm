"""pydantic's classes are imported on first use: before anything imports pydantic, no value is taken
for a pydantic model, and nothing is imported to find that out."""

import sys

import pytest
from pydantic import BaseModel, TypeAdapter

from hare.utils.pydantic_classes import PydanticClasses


class Point(BaseModel):
    x: int


@pytest.fixture
def unloaded_pydantic_classes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(PydanticClasses, "loaded", False)
    monkeypatch.setattr(PydanticClasses, "base_model", None)
    monkeypatch.setattr(PydanticClasses, "type_adapter", None)


def test_nothing_is_imported_before_pydantic_is(unloaded_pydantic_classes, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delitem(sys.modules, "pydantic")
    assert PydanticClasses.is_model_instance(object()) is False
    assert PydanticClasses.loaded is False


def test_the_classes_are_taken_once_pydantic_is_imported(unloaded_pydantic_classes):
    assert PydanticClasses.is_model_instance(Point(x=1)) is True
    assert PydanticClasses.loaded is True
    assert PydanticClasses.is_model_class(Point) is True
    assert PydanticClasses.is_model_class(Point(x=1)) is False
    assert PydanticClasses.is_type_adapter(TypeAdapter(int)) is True
    assert PydanticClasses.is_type_adapter(int) is False


def test_the_type_adapter_is_imported_when_asked_for(unloaded_pydantic_classes):
    assert PydanticClasses.get_type_adapter() is TypeAdapter
    assert PydanticClasses.base_model is BaseModel
