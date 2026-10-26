"""Cast() of a number or a boolean on SQLite is SQLite's own CAST - hare checks nothing: text that is no
number is 0, a fraction is cut off; a boolean is the value read as a number, true when it isn't 0. An
integer field set from a fraction is cast as SQLite casts - an INTEGER column kept it as a REAL."""

import pytest

from hare import fields
from hare.query.expressions import F, Value
from hare.query.functions import Cast
from tests.shared_models.testmodels import CharFields, IntFields

pytestmark = pytest.mark.sqlite_only


async def get_value(expression):
    return await CharFields.objects.annotate(value=expression).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_a_number_and_a_boolean_cast_as_sqlite_casts(db):
    await CharFields.objects.create(id=1, char=" 42 ")

    for expression, expected in (
        (Cast("char", fields.IntField()), 42),
        (Cast(Value("abc"), fields.IntField()), 0),
        (Cast(Value("4.9"), fields.IntField()), 4),
        (Cast(Value(-1.9), fields.BigIntField()), -1),
        (Cast(Value("1.5"), fields.FloatField()), 1.5),
        (Cast(Value("abc"), fields.FloatField()), 0.0),
        (Cast(Value("true"), fields.BooleanField()), False),
        (Cast(Value("2"), fields.BooleanField()), True),
        (Cast(Value(0.0), fields.BooleanField()), False),
        (Cast(Value(None), fields.IntField()), None),
    ):
        assert await get_value(expression) == [expected]


@pytest.mark.asyncio
async def test_an_integer_field_set_from_a_fraction_is_cast_as_sqlite_casts(db):
    await IntFields.objects.create(id=1, intnum=1)

    await IntFields.objects.filter(id=1).update(intnum=F("intnum") * 2.6)
    assert await IntFields.objects.values_list("intnum", flat=True) == [2]
    await IntFields.objects.filter(id=1).update(intnum=F("intnum") - 4.5)
    assert await IntFields.objects.values_list("intnum", flat=True) == [-2]
