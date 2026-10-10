import pytest

from hare.exceptions import IntegrityError, ValidationError
from hare.fields import BinaryField
from hare.transactions.transactions import Transactions
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


def test_unique_and_index_are_taken():
    assert BinaryField(unique=True).unique
    assert BinaryField(db_index=True).index
    assert BinaryField(primary_key=True).pk


@pytest.mark.asyncio
async def test_a_unique_and_an_indexed_binary_field(db):
    model = testmodels.IndexedBinaryFields
    await model.objects.create(id=1, digest=b"\x00\x01", tag=b"a")
    await model.objects.create(id=2, digest=b"\x00\x02", tag=b"b")
    if model.get_connection().features.supports_unique_constraints:
        with pytest.raises(IntegrityError):
            # A savepoint - the failed insert leaves the test's transaction usable.
            async with Transactions.atomic():
                await model.objects.create(id=3, digest=b"\x00\x01")
    assert await model.objects.filter(digest=b"\x00\x02").values_list("id", flat=True) == [2]
    assert await model.objects.filter(digest__gt=b"\x00\x01").values_list("id", flat=True) == [2]
    assert await model.objects.filter(tag__in=[b"a", b"z"]).values_list("id", flat=True) == [1]
    assert await model.objects.filter(id=1).update(tag=b"c") == 1
    assert await model.objects.get(id=1).values_list("tag", flat=True) == b"c"
    indexed_columns = {tuple(index.fields) for index in model._meta.indexes} | {
        (field_name,) for field_name, field in model._meta.fields_map.items() if field.index or field.unique
    }
    assert ("digest",) in indexed_columns and ("tag",) in indexed_columns
