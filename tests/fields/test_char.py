import pytest

from hare import fields
from hare.exceptions import ConfigurationError, ValidationError
from tests import testmodels


def test_max_length_missing():
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'max_length'"):
        fields.CharField()  # pylint: disable=E1120


def test_max_length_bad():
    with pytest.raises(ConfigurationError, match="'max_length' must be >= 1"):
        fields.CharField(max_length=0)


@pytest.mark.asyncio
async def test_empty(db):
    with pytest.raises(ValidationError):
        await testmodels.CharFields.objects.create()


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.CharFields.objects.create(char="moo")
    obj = await testmodels.CharFields.objects.get(id=obj0.id)
    assert obj.char == "moo"
    assert obj.char_null is None
    await obj.save()
    obj2 = await testmodels.CharFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_update(db):
    obj0 = await testmodels.CharFields.objects.create(char="moo")
    await testmodels.CharFields.objects.filter(id=obj0.id).update(char="ba'a")
    obj = await testmodels.CharFields.objects.get(id=obj0.id)
    assert obj.char == "ba'a"
    assert obj.char_null is None


@pytest.mark.asyncio
async def test_cast(db):
    obj0 = await testmodels.CharFields.objects.create(char=33)
    obj = await testmodels.CharFields.objects.get(id=obj0.id)
    assert obj.char == "33"


@pytest.mark.asyncio
async def test_values(db):
    obj0 = await testmodels.CharFields.objects.create(char="moo")
    values = await testmodels.CharFields.objects.get(id=obj0.id).values("char")
    assert values["char"] == "moo"


@pytest.mark.asyncio
async def test_values_list(db):
    obj0 = await testmodels.CharFields.objects.create(char="moo")
    values = await testmodels.CharFields.objects.get(id=obj0.id).values_list("char", flat=True)
    assert values == "moo"


def test_constructing_with_a_str_skips_the_assignment_coercion(monkeypatch):
    """Bug: Model(**kwargs) ran each value through Expression/callable checks and the field's
    to_python() even when it already was the type that call returns unchanged -
    about a third of the cost of building an object. A value of another type is still coerced."""
    calls = []
    original_coercion = fields.CharField.to_python

    def counting_coercion(self, value):
        calls.append(value)
        return original_coercion(self, value)

    monkeypatch.setattr(fields.CharField, "to_python", counting_coercion)
    instance = testmodels.CharFields(char="plain", char_null="text")
    assert (instance.char, instance.char_null) == ("plain", "text")
    assert calls == []

    coerced = testmodels.CharFields(char=123)
    assert coerced.char == "123"
    assert calls == [123]
