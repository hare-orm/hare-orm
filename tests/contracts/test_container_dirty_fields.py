"""Dirty tracking compares held JSON types and the declared document representation."""

from decimal import Decimal

import pytest

from hare import Model, fields
from hare.contrib.test import capture_queries
from hare.exceptions import UnSupportedError
from hare.models.instances.field_snapshot import FieldSnapshot
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.container_dirty_samples import ContainerDirtySamples


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["root", "tuple", "nested-tuple", "named", "array", "map", "nested"])
@pytest.mark.parametrize(
    "typed", [False, pytest.param(True, marks=pytest.mark.requires("pydantic"))], ids=["plain-json", "typed-json"]
)
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_held_document_type_changes_are_written(contract_database, shape, typed, composite):
    declaration, payload, path = ContainerDirtySamples.get_sample(shape, typed)
    connection = contract_database.get_connection()
    try:
        declaration.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no column type for this container")

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        document = declaration

        class Meta:
            track_dirty_fields = True

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, document=payload)
    obj = await Book.objects.get()
    snapshot = obj.snapshot()
    document = ContainerDirtySamples.get_document(obj.document, path)
    if typed:
        document.flag = True
    else:
        document["flag"] = True
    assert set(obj.get_dirty_fields()) == {"document"}
    assert set(obj.diff_against(snapshot)) == {"document"}
    await obj.save(changed_only=True)
    assert obj.get_dirty_fields() == {}
    fetched = await Book.objects.get()
    document = ContainerDirtySamples.get_document(fetched.document, path)
    assert (document.flag if typed else document["flag"]) is True


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "declaration,old,new",
    [
        (fields.ArrayField(fields.DecimalField(max_digits=8, decimal_places=2)), [Decimal("1")], [Decimal("1.0")]),
        (fields.TupleField([fields.DecimalField(max_digits=8, decimal_places=2)]), (Decimal("1"),), (Decimal("1.0"),)),
    ],
)
def test_container_comparison_preserves_numeric_field_equality(declaration, old, new):
    assert not FieldSnapshot.values_differ(
        declaration.get_snapshot_comparison_value(old), declaration.get_snapshot_comparison_value(new)
    )


@pytest.mark.asyncio
async def test_a_nested_nan_read_back_does_not_trigger_a_write(contract_database):
    declaration = fields.ArrayField(fields.ArrayField(fields.FloatField()))
    connection = contract_database.get_connection()
    try:
        declaration.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no array type")

    class Reading(Model):
        number = fields.IntField(primary_key=True, generated=False)
        vector = declaration

        class Meta:
            track_dirty_fields = True

    contract_database.register_live_models([Reading], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Reading.objects.create(number=1, vector=[[float("nan"), 2.0]])
    first = await Reading.objects.get(pk=1)
    second = await Reading.objects.get(pk=1)
    snapshot = first.snapshot()

    first.vector = second.vector
    assert first.get_dirty_fields() == {}
    assert first.diff_against(snapshot) == {}
    async with capture_queries() as queries:
        await first.save(changed_only=True)
    assert queries.count == 0

    first.vector[0][1] = 3.0
    assert set(first.get_dirty_fields()) == {"vector"}
    assert set(first.diff_against(snapshot)) == {"vector"}
