import pytest

from hare.exceptions import ConfigurationError, IntegrityError, ValidationError
from hare.fields import BinaryField
from tests import testmodels


@pytest.mark.asyncio
async def test_empty(db):
    with pytest.raises(IntegrityError):
        await testmodels.BinaryFields.objects.create()


@pytest.mark.asyncio
async def test_int_value_raises_instead_of_becoming_n_zero_bytes(db):
    # bytes(5) means "5 zero bytes" in Python, not "the bytes representation of the int 5" - a
    # trap the base Field.to_db_value()/from_db_value() generic coercion fell into silently.
    with pytest.raises(ValidationError, match="expected bytes-like data"):
        await testmodels.BinaryFields.objects.create(binary=5)


@pytest.mark.asyncio
async def test_bytearray_still_coerces(db):
    obj = await testmodels.BinaryFields.objects.create(binary=bytearray(b"\xaa\xbb"))
    assert obj.binary == b"\xaa\xbb"


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.BinaryFields.objects.create(binary=bytes(range(256)) * 500)
    obj = await testmodels.BinaryFields.objects.get(id=obj0.id)
    assert obj.binary == bytes(range(256)) * 500
    assert obj.binary_null is None
    await obj.save()
    obj2 = await testmodels.BinaryFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_values(db):
    obj0 = await testmodels.BinaryFields.objects.create(
        binary=bytes(range(256)), binary_null=bytes(range(255, -1, -1))
    )
    values = await testmodels.BinaryFields.objects.get(id=obj0.id).values("binary", "binary_null")
    assert values["binary"] == bytes(range(256))
    assert values["binary_null"] == bytes(range(255, -1, -1))


@pytest.mark.asyncio
async def test_values_list(db):
    obj0 = await testmodels.BinaryFields.objects.create(binary=bytes(range(256)))
    values = await testmodels.BinaryFields.objects.get(id=obj0.id).values_list("binary", flat=True)
    assert values == bytes(range(256))


def test_unique_fail():
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        BinaryField(unique=True)


def test_index_fail():
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        BinaryField(db_index=True)
