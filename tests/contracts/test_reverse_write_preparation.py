"""Reverse writes compare accepted keys and undo preparation with the write."""

from uuid import UUID

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.exceptions import IntegrityError, QueryError
from hare.transactions.transactions import Transactions
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("operation", ["create", "get", "update", "bulk"])
async def test_reverse_writes_accept_the_parents_uuid_string(contract_database, composite, operation):
    connection = contract_database.get_connection()

    class Author(Model):
        code = fields.UUIDField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "code")

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField(
            f"{CONTRACT_APP}.Author", related_name="books", db_constraint=connection.features.supports_foreign_keys
        )

    contract_database.register_live_models([Author, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    code = UUID("12345678-1234-4234-9234-123456789abc")
    author = await Author.objects.create(code=code)
    relation = HareContext.require_current().apps.get_binding(Book).fields_map["author"]
    given = dict(zip(relation.source_fields, (7, str(code)) if composite else (str(code),), strict=True))
    if operation == "create":
        result = await author.books.create(number=1, **given)
    elif operation == "get":
        result, created = await author.books.get_or_create(number=1, **given)
        assert created
    elif operation == "update":
        result, created = await author.books.update_or_create(number=1, **given)
        assert created
    else:
        result = Book(number=1, **given)
        await author.books.bulk_create([result], returning=False)
    assert tuple(getattr(result, name) for name in relation.source_fields) == ((7, code) if composite else (code,))
    assert await Book.objects.count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("failure", ["preparation", "sql", "rollback"])
async def test_reverse_bulk_insert_restores_parent_keys_on_failure(contract_database, composite, failure):
    connection = contract_database.get_connection()
    if failure in {"sql", "rollback"} and not connection.features.supports_transactions:
        pytest.skip("SQL rollback requires transactions")

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
            related_name="books",
            db_constraint=connection.features.supports_foreign_keys,
        )

    contract_database.register_live_models([Author, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    author = await Author.objects.create(number=1)
    other = await Author.objects.create(number=2)
    given = Book(number=3, author=None)
    if failure == "preparation":
        query = author.books.bulk_create([given, Book(number=4, author=other)], returning=False)
        with pytest.raises(QueryError, match="parent instance"):
            await query
    elif failure == "sql":
        await Book.objects.create(number=3, author=None)
        with pytest.raises(IntegrityError):
            await author.books.bulk_create([given], returning=False)
    else:
        with pytest.raises(RuntimeError, match="rollback"):
            async with Transactions.atomic(using=CONTRACT_CONNECTION):
                await author.books.bulk_create([given], returning=False)
                raise RuntimeError("rollback")
    assert all(
        getattr(given, name) is None
        for name in HareContext.require_current().apps.get_binding(Book).fields_map["author"].source_fields
    )
    assert not given._saved_in_db
    assert await Book.objects.count() == (1 if failure == "sql" else 0)
