import pytest

from hare.exceptions import IntegrityError
from hare.fields import TextField
from tests import testmodels


@pytest.mark.asyncio
async def test_empty(db):
    with pytest.raises(IntegrityError):
        await testmodels.TextFields.objects.create()


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.TextFields.objects.create(text="baaa" * 32000)
    obj = await testmodels.TextFields.objects.get(id=obj0.id)
    assert obj.text == "baaa" * 32000
    assert obj.text_null is None
    await obj.save()
    obj2 = await testmodels.TextFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_values(db):
    obj0 = await testmodels.TextFields.objects.create(text="baa")
    values = await testmodels.TextFields.objects.get(id=obj0.id).values("text")
    assert values["text"] == "baa"


@pytest.mark.asyncio
async def test_values_list(db):
    obj0 = await testmodels.TextFields.objects.create(text="baa")
    values = await testmodels.TextFields.objects.get(id=obj0.id).values_list("text", flat=True)
    assert values == "baa"


def test_index_and_unique_accepted():
    assert TextField(db_index=True).index
    assert TextField(unique=True).unique


def test_primary_key_accepted():
    assert TextField(primary_key=True).pk
