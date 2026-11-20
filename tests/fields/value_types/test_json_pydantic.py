"""JSONField with pydantic: a field_type that is a pydantic model, an encoder writing one."""

import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.ddl.indexes import Index
from hare.exceptions import ValidationError
from hare.fields import JSONField
from tests.shared_models import testmodels, testmodels_pydantic
from tests.utils.database_under_test import DatabaseUnderTest

pytestmark = pytest.mark.usefixtures("model_conversion_context")


class MyPydanticModel(BaseModel):
    name: str
    idx: Index
    model_config = dict(arbitrary_types_allowed=True)


@pytest.mark.asyncio
async def test_data_pydantic_shape_mismatch_raises_validation_error_on_create(db):
    """JSONField(field_type=SomePydanticModel) used to validate the value's shape only on READ
    (from_db_value), not on WRITE (to_db_value) - a dict that doesn't match the pydantic
    model's schema used to be written successfully, only failing later, when some other read
    re-runs pydantic validation. Must now fail immediately at create() time."""
    with pytest.raises(ValidationError):
        await testmodels_pydantic.PydanticJSONFields.objects.create(
            data=[], data_pydantic={"foo": "not an int", "bar": "baz"}
        )

    with pytest.raises(ValidationError):
        await testmodels_pydantic.PydanticJSONFields.objects.create(
            data=[], data_pydantic={"foo": 1}
        )  # missing required "bar"


@pytest.mark.asyncio
async def test_data_pydantic_shape_mismatch_raises_validation_error_on_update(db):
    """Same shape validation as create() must also run on the QuerySet.update() write path."""
    obj = await testmodels_pydantic.PydanticJSONFields.objects.create(data=[])
    with pytest.raises(ValidationError):
        await testmodels_pydantic.PydanticJSONFields.objects.filter(pk=obj.pk).update(
            data_pydantic={"foo": "not an int", "bar": "baz"}
        )


@pytest.mark.asyncio
async def test_data_pydantic_matching_shape_still_succeeds(db):
    """A plain dict matching the configured pydantic model's shape must still write and read
    back correctly - the new write-time validation must not reject valid data."""
    obj0 = await testmodels_pydantic.PydanticJSONFields.objects.create(data=[], data_pydantic={"foo": 1, "bar": "baz"})
    obj = await testmodels_pydantic.PydanticJSONFields.objects.get(id=obj0.id)
    assert obj.data_pydantic.foo == 1
    assert obj.data_pydantic.bar == "baz"


@pytest.mark.database_independent
def test_to_db_value_pydantic_field_type_shape_mismatch_raises_immediately():
    """Direct to_db_value() call (no DB round trip) - the shape mismatch must be caught before
    any encoding/DB interaction happens at all, not merely by the time a full create() runs."""
    field = testmodels_pydantic.PydanticJSONFields._declaration.fields_map["data_pydantic"]
    with pytest.raises(ValidationError):
        field.to_db_value({"foo": "not an int", "bar": "baz"}, testmodels_pydantic.PydanticJSONFields)


@pytest.mark.asyncio
async def test_declared_field_type_validates_and_converts_on_assign_write_and_read(db):
    """field_type=Model / list[Model]: a dict (or list of dicts) becomes the model in memory
    right away, the same value a later read returns; list items are validated too."""
    schema_type = testmodels_pydantic.TestSchemaForJSONField
    row = await testmodels_pydantic.JSONFieldsDeclaredType.objects.create(
        item={"foo": "1", "bar": "a"}, items=[schema_type(foo=2, bar="b"), {"foo": 3, "bar": "c"}]
    )

    assert row.item == schema_type(foo=1, bar="a")
    assert row.items == [schema_type(foo=2, bar="b"), schema_type(foo=3, bar="c")]
    fetched = await testmodels_pydantic.JSONFieldsDeclaredType.objects.get(id=row.id)
    assert fetched.item == row.item
    assert fetched.items == row.items
    values = await testmodels_pydantic.JSONFieldsDeclaredType.objects.filter(id=row.id).values("items")
    assert values == [{"items": row.items}]

    with pytest.raises(ValidationError):
        await testmodels_pydantic.JSONFieldsDeclaredType.objects.create(items=[{"foo": "not an int", "bar": "x"}])
    with pytest.raises(ValidationError):
        await testmodels_pydantic.JSONFieldsDeclaredType.objects.create(item={"bar": "missing foo"})


@pytest.mark.asyncio
async def test_an_encoder_writes_a_pydantic_model(db):
    """A JSONField's own encoder writes a pydantic model holding a hare object - read back as the JSON it
    wrote."""
    pyd_model = MyPydanticModel(name="", idx=Index(fields=["data"]))
    obj0 = await testmodels.JSONFields.objects.create(data="{}", data_index=pyd_model)
    obj = await testmodels.JSONFields.objects.get(id=obj0.id)
    assert obj.data_index == {"idx": {"fields": ["data"]}, "name": ""}


class JsonDocument(BaseModel):
    title: str


@pytest.mark.database_independent
def test_deconstruct_writes_a_declared_field_type():
    """A declared field_type is written into the migration as it is."""
    _, _, declared_kwargs = JSONField(field_type=JsonDocument).deconstruct()

    assert declared_kwargs["field_type"] is JsonDocument


@pytest.mark.database_independent
def test_a_stored_non_object_of_a_model_field_type_raises_validation_error():
    """A stored value that decodes to valid JSON but isn't a dict (schema drift, a manual DB edit, a row
    written before field_type was added) reached a bare `self.field_type(**data)` call - a TypeError."""
    field = testmodels_pydantic.PydanticJSONFields._declaration.fields_map["data_pydantic"]
    with pytest.raises(ValidationError):
        field.from_db_value("5")


class MutableDocument(BaseModel):
    data: dict[str, Any]


@dataclass
class MutableDataclassDocument:
    data: dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_value", (True, 1.0, 2), ids=("bool", "float", "changed-integer"))
@pytest.mark.parametrize("json_field", (fields.JSONField, fields.JSONTextField))
async def test_dirty_tracking_compares_the_json_form_of_typed_documents(monkeypatch, new_value, json_field):
    if json_field is fields.JSONField and DatabaseUnderTest.get_dialect().name == "clickhouse":
        pytest.skip("A native JSON column holds an object, not a top-level document list")

    class Document(Model):
        id = fields.IntField(primary_key=True, generated=False)
        item = json_field(field_type=MutableDocument)
        items = json_field(field_type=list[MutableDocument])
        by_name = json_field(field_type=dict[str, MutableDocument])
        dataclass_item = json_field(field_type=MutableDataclassDocument)
        plain = json_field()

        class Meta:
            track_dirty_fields = True

    module = ModuleType("tests._typed_document_dirty_fields")
    module.__models__ = [Document]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        value = {"data": {"values": [1]}}
        obj = await Document.objects.create(
            id=1, item=value, items=[value], by_name={"entry": value}, dataclass_item=value, plain=value
        )
        baseline = obj.snapshot()
        for data in (
            obj.item.data,
            obj.items[0].data,
            obj.by_name["entry"].data,
            obj.dataclass_item.data,
            obj.plain["data"],
        ):
            data["values"][0] = new_value
        expected_fields = {"item", "items", "by_name", "dataclass_item", "plain"}
        assert set(obj.get_dirty_fields()) == set(obj.diff_against(baseline)) == expected_fields
        assert type(baseline["item"].data["values"][0]) is int
        assert type(baseline["dataclass_item"].data["values"][0]) is int
        await obj.save(changed_only=True)
        stored = await Document.objects.get(id=1)
        for data in (
            stored.item.data,
            stored.items[0].data,
            stored.by_name["entry"].data,
            stored.dataclass_item.data,
            stored.plain["data"],
        ):
            assert type(data["values"][0]) is type(new_value)
            assert data["values"][0] == new_value
        assert obj.get_dirty_fields() == {}
