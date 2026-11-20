"""Partial application preserves async defaults for row writes and SET_DEFAULT."""

from functools import partial

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.fields.enums import OnDelete
from tests.contracts.async_default_provider import AsyncDefaultProvider
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("operation", ["create", "save", "bulk", "explicit"])
@pytest.mark.parametrize("wrap_method", [False, True], ids=["callable-object", "bound-method"])
async def test_partially_applied_async_objects_supply_defaults(
    contract_database, monkeypatch, composite, operation, wrap_method
):
    monkeypatch.setattr(AsyncDefaultProvider, "calls", [])
    provider = AsyncDefaultProvider()
    default = partial(provider.__call__ if wrap_method else provider, 17)

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        amount = fields.IntField(default=default)

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    if operation in {"create", "explicit"}:
        obj = await Book.objects.create(number=1, **({"amount": 19} if operation == "explicit" else {}))
    else:
        obj = Book(number=1)
        if operation == "save":
            await obj.save()
        else:
            await Book.objects.bulk_create([obj], returning=False)
    assert AsyncDefaultProvider.calls == ([] if operation == "explicit" else [17])
    assert obj.amount == (19 if operation == "explicit" else 17)
    assert await Book.objects.values_list("amount", flat=True) == [obj.amount]


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("null_default", [False, True], ids=["target-key", "null-key"])
async def test_set_default_awaits_partially_applied_async_object(
    contract_database, monkeypatch, composite, null_default
):
    monkeypatch.setattr(AsyncDefaultProvider, "calls", [])
    default_key = None if null_default else (7, 2) if composite else 2

    class Author(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField(
            f"{CONTRACT_APP}.Author",
            null=True,
            on_delete=OnDelete.SET_DEFAULT,
            default=partial(AsyncDefaultProvider(), default_key),
            db_constraint=False,
        )

    contract_database.register_live_models([Author, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    first = await Author.objects.create(number=1)
    second = await Author.objects.create(number=2)
    await Book.objects.create(number=1, author=first)
    await first.delete()
    assert AsyncDefaultProvider.calls == [default_key]
    fetched = await Book.objects.get()
    values = tuple(
        getattr(fetched, name)
        for name in HareContext.require_current().apps.get_binding(Book).fields_map["author"].source_fields
    )
    expected = ((None, None) if composite else (None,)) if null_default else ((7, 2) if composite else (2,))
    assert values == expected
    assert await Author.objects.count() == 1
    assert second.pk is not None
