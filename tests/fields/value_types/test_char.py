import uuid
from decimal import Decimal

import pytest

from hare import fields
from hare.exceptions import ConfigurationError, ValidationError
from tests.shared_models import testmodels


@pytest.mark.database_independent
def test_max_length_missing():
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'max_length'"):
        fields.CharField()  # pylint: disable=E1120


@pytest.mark.database_independent
def test_max_length_bad():
    with pytest.raises(ConfigurationError, match="'max_length' must be >= 1"):
        fields.CharField(max_length=0)


@pytest.mark.database_independent
@pytest.mark.parametrize("max_length", ["5", True, 2.5])
def test_max_length_of_another_type_is_refused(max_length):
    """int() took "5" and True for a length."""
    with pytest.raises(ConfigurationError, match="'max_length' must be an int"):
        fields.CharField(max_length=max_length)


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"description": 5}, "CharField: description must be a text or None, got 5"),
        ({"source_field": 5}, "CharField: source_field must be a text or None"),
    ],
)
def test_a_parameter_of_another_type_is_refused(parameters, message):
    """validators=5 failed with an AttributeError."""
    with pytest.raises(ConfigurationError, match=message):
        fields.CharField(max_length=10, **parameters)


@pytest.mark.database_independent
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

    with pytest.raises(ValidationError):
        testmodels.CharFields(char=123)
    assert calls == [123]


@pytest.mark.asyncio
async def test_a_text_holding_a_lone_surrogate_is_refused(db):
    """Half of a surrogate pair has no UTF-8 encoding - each driver failed on it in its own way: a
    UnicodeEncodeError here, a driver's error holding the text there."""
    text = "ab\ud800cd"
    for write in (
        lambda: testmodels.CharFields.objects.create(char=text),
        lambda: testmodels.CharFields.objects.bulk_create([testmodels.CharFields(char=text)]),
        lambda: testmodels.CharFields.objects.filter(char=text).count(),
    ):
        with pytest.raises(ValidationError, match="lone surrogate") as error_info:
            await write()
        assert error_info.value.__cause__ is None
    assert await testmodels.CharFields.objects.count() == 0


@pytest.mark.parametrize("value", [True, b"x", [1], {"a": 1}, {1}, object()])
@pytest.mark.asyncio
async def test_a_value_that_is_no_text_is_refused_not_stored_as_its_str(db, value):
    """A text field stored the str() of anything - bytes as "b'x'", a list as "[1]", True as "True"."""
    with pytest.raises(ValidationError, match="char: expected a str"):
        await testmodels.CharFields.objects.create(id=1, char=value)
    await testmodels.CharFields.objects.create(id=2, char="a")
    with pytest.raises(ValidationError, match="char: expected a str"):
        await testmodels.CharFields.objects.filter(id=2).update(char=value)
    with pytest.raises(ValidationError, match="text: expected a str"):
        await testmodels.TextFields.objects.create(id=1, text=value)
    assert await testmodels.CharFields.objects.values_list("char", flat=True) == ["a"]


@pytest.mark.asyncio
async def test_a_number_or_a_uuid_is_no_text(db):
    """A number or a UUID was written as its text - the write and a text lookup take only a str."""
    for value in (5, 1.5, Decimal("2"), uuid.UUID(int=1)):
        with pytest.raises(ValidationError, match="expected a str"):
            await testmodels.CharFields.objects.create(id=1, char=value)
        with pytest.raises(ValidationError):
            await testmodels.CharFields.objects.filter(char=value).count()
        with pytest.raises(ValidationError, match="matches a text"):
            await testmodels.CharFields.objects.filter(char__icontains=value).count()
    with pytest.raises(ValidationError, match="matches a text"):
        await testmodels.IntFields.objects.filter(intnum__contains=5).count()
    assert await testmodels.IntFields.objects.filter(intnum__contains="5").count() == 0
    assert await testmodels.CharFields.objects.count() == 0
