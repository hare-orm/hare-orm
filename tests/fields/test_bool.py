import pytest

from hare.exceptions import IntegrityError, ValidationError
from tests import testmodels


@pytest.mark.asyncio
async def test_empty(db):
    with pytest.raises(IntegrityError):
        await testmodels.BooleanFields.objects.create()


@pytest.mark.asyncio
async def test_string_value_raises_instead_of_truthy_coercion(db):
    # bool("false") is True - any non-empty string is truthy regardless of its content, a trap
    # the base Field.to_db_value()/from_db_value() generic coercion fell into silently.
    with pytest.raises(ValidationError, match="expected a bool"):
        await testmodels.BooleanFields.objects.create(boolean="false")


@pytest.mark.asyncio
async def test_int_value_still_coerces(db):
    # A raw SQLite INT column round-trips through this same coercion on read - 0/1 must keep
    # working.
    obj_true = await testmodels.BooleanFields.objects.create(boolean=1)
    obj_false = await testmodels.BooleanFields.objects.create(boolean=0)
    assert obj_true.boolean is True
    assert obj_false.boolean is False


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.BooleanFields.objects.create(boolean=True)
    obj = await testmodels.BooleanFields.objects.get(id=obj0.id)
    assert obj.boolean is True
    assert obj.boolean_null is None
    await obj.save()
    obj2 = await testmodels.BooleanFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_update(db):
    obj0 = await testmodels.BooleanFields.objects.create(boolean=False)
    await testmodels.BooleanFields.objects.filter(id=obj0.id).update(boolean=False)
    obj = await testmodels.BooleanFields.objects.get(id=obj0.id)
    assert obj.boolean is False
    assert obj.boolean_null is None


@pytest.mark.asyncio
async def test_values(db):
    obj0 = await testmodels.BooleanFields.objects.create(boolean=True)
    values = await testmodels.BooleanFields.objects.get(id=obj0.id).values("boolean")
    assert values["boolean"] is True


@pytest.mark.asyncio
async def test_values_list(db):
    obj0 = await testmodels.BooleanFields.objects.create(boolean=True)
    values = await testmodels.BooleanFields.objects.get(id=obj0.id).values_list("boolean", flat=True)
    assert values is True
