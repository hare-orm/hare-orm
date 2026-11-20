"""Conflict grouping, reporting and RETURNING consume one set of converted rows."""

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.observers.observers import Observers
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.single_pass_key import SinglePassKey
from tests.contracts.single_pass_key_field import SinglePassKeyField
from tests.contracts.single_pass_number_field import SinglePassNumberField


@pytest.mark.asyncio
@requires_features(supports_returning=True, supports_unique_constraints=True)
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("operation", ["ignore", "upsert", "upsert-no-return"])
@pytest.mark.parametrize("observed", [False, True], ids=["ordinary", "reported"])
async def test_bulk_conflicts_reuse_bound_input_values(contract_database, monkeypatch, composite, operation, observed):
    monkeypatch.setattr(SinglePassNumberField, "consumed", [])

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=3)
            pk = fields.CompositePrimaryKey("region", "number")
        code = SinglePassNumberField(unique=True, source_field="stored_code")
        amount = fields.IntField()

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, code=11, amount=10)
    objects = [
        Book(number=2, code=iter([11]), amount=20),
        Book(number=3, code=iter([22]), amount=30),
        Book(number=4, code=iter([11]), amount=40),
    ]
    changes = []
    listener = Observers.observe(RowsChanged, changes.append, models=[Book]) if observed else None
    try:
        result = await Book.objects.bulk_create(
            objects,
            ignore_conflicts=operation == "ignore",
            update_fields=["amount"] if operation != "ignore" else None,
            on_conflict=["code"],
            returning=operation != "upsert-no-return",
        )
    finally:
        if listener is not None:
            Observers.unobserve(RowsChanged, listener)
    assert SinglePassNumberField.consumed == [[11], [22], [11]]
    assert await Book.objects.order_by("number").values_list("number", "code", "amount") == [
        (1, 11, 10 if operation == "ignore" else 40),
        (3, 22, 30),
    ]
    assert result == ([objects[1]] if operation == "ignore" else objects)
    assert [obj._saved_in_db for obj in objects] == (
        [False, True, False] if operation == "ignore" else [True, True, True]
    )
    assert bool(changes) is observed


@pytest.mark.asyncio
@requires_features(supports_returning=True)
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_bulk_primary_keys_are_bound_once(contract_database, monkeypatch, composite, operation):
    monkeypatch.setattr(SinglePassKeyField, "consumed", [])

    class Book(Model):
        code = SinglePassKeyField(primary_key=not composite, generated=False, source_field="stored_key")
        if composite:
            region = fields.IntField(default=3)
            pk = fields.CompositePrimaryKey("region", "code")
        amount = fields.IntField()

        class Meta:
            track_dirty_fields = True

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    objects = [Book(code=SinglePassKey(11), amount=1), Book(code=SinglePassKey(22), amount=2)]
    if operation == "update":
        for obj in objects:
            await obj.save()
        objects = await Book.objects.order_by("code")
        for obj in objects:
            obj.amount += 10
        SinglePassKeyField.consumed.clear()
        assert await Book.objects.bulk_update(objects, ["amount"], returning=True) == 2
        assert all(obj.get_dirty_fields() == {} for obj in objects)
    else:
        assert await Book.objects.bulk_create(objects, returning=True) == objects
    assert SinglePassKeyField.consumed == [[11], [22]]
    connection = contract_database.get_connection()
    assert await connection.execute_dicts("SELECT stored_key, amount FROM book ORDER BY stored_key") == [
        {"stored_key": 11, "amount": 11 if operation == "update" else 1},
        {"stored_key": 22, "amount": 12 if operation == "update" else 2},
    ]
    assert all(obj._saved_in_db for obj in objects)
