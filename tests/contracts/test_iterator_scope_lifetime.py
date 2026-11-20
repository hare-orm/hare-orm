"""A paged execution retains its tenant while yielding outside that scope."""

import datetime
from contextlib import nullcontext

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F
from hare.transactions import Transactions
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def scoped_records(contract_database, composite):
    class Record(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        tenant = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            tenant_field = "tenant"
            soft_delete_field = "deleted_at"

    class Note(Model):
        number = fields.IntField(primary_key=True, generated=False)
        record = fields.ForeignKeyField(Record, related_name="notes", db_constraint=False)
        tenant = fields.IntField()

        class Meta:
            tenant_field = "tenant"

    contract_database.register_live_models([Record, Note], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    with Tenancy.scope(Tenancy.ALL):
        records = [
            Record(
                number=number,
                tenant=1 if number % 2 else 2,
                deleted_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC) if number >= 7 else None,
            )
            for number in range(1, 13)
        ]
        await Record.objects.bulk_create(records)
        await Note.objects.bulk_create([Note(number=obj.number, record=obj, tenant=obj.tenant) for obj in records[:4]])
    return Record


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("visibility", ["live", "include", "only"])
@pytest.mark.parametrize("consumer_scope", [None, 2, "all", "model-scopes"])
async def test_iterator_retains_its_starting_scope_and_restores_the_consumer(
    scoped_records, flat, visibility, consumer_scope
):
    queryset = scoped_records.objects.order_by("number")
    if flat:
        queryset = (
            queryset.annotate(sort_number=F("number") + 0).order_by("sort_number").values_list("number", flat=True)
        )
    if visibility == "include":
        queryset = queryset.include_deleted()
    elif visibility == "only":
        queryset = queryset.only_deleted()
    with Tenancy.scope(99):
        iterator = queryset.iterator(chunk_size=1)
    with Tenancy.scope(1):
        first = await anext(iterator)
    expected = [1, 3, 5] if visibility == "live" else [1, 3, 5, 7, 9, 11] if visibility == "include" else [7, 9, 11]
    scope = (
        Tenancy.ALL
        if consumer_scope == "all"
        else {scoped_records: 2}
        if consumer_scope == "model-scopes"
        else consumer_scope
    )
    with Tenancy.scope(scope):
        active_scope = Tenancy.current.get()
        numbers = [first if flat else first.number]
        async for obj in iterator:
            assert Tenancy.current.get() is active_scope
            numbers.append(obj if flat else obj.number)
        assert numbers == expected
        assert Tenancy.current.get() is active_scope
        if consumer_scope is None:
            with pytest.raises(QueryError):
                await queryset
        else:
            fresh = await queryset
            fresh_numbers = [obj if flat else obj.number for obj in fresh]
            assert fresh_numbers == (
                sorted([*expected, *(number + 1 for number in expected)])
                if scope is Tenancy.ALL
                else [number + 1 for number in expected]
            )
        await iterator.aclose()
        assert Tenancy.current.get() is active_scope


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("method", ["iterator", "stream"])
async def test_scoped_reader_closes_early_under_the_consumers_scope(contract_database, scoped_records, method):
    queryset = scoped_records.objects.order_by("number").values_list("number", flat=True)
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if method == "stream" and not connection.features.supports_streaming:
        pytest.skip("this client does not stream rows")
    scope = (
        Transactions.atomic(using=CONTRACT_CONNECTION)
        if method == "stream" and not connection.features.streams_without_transaction
        else nullcontext()
    )
    async with scope:
        reader = getattr(queryset, method)(chunk_size=1)
        try:
            with Tenancy.scope(1):
                assert await anext(reader) == 1
            with Tenancy.scope(2):
                assert await anext(reader) == 3
                assert Tenancy.current.get() == 2
                await reader.aclose()
                assert Tenancy.current.get() == 2
                with pytest.raises(StopAsyncIteration):
                    await anext(reader)
                assert await queryset == [2, 4, 6]
        finally:
            await reader.aclose()


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_iterator_prefetch_uses_the_initial_scope_for_each_page(scoped_records):
    query = scoped_records.objects.order_by("number").prefetch_related("notes")
    reader = query.iterator(chunk_size=1)
    try:
        with Tenancy.scope(1):
            first = await anext(reader)
            assert (first.number, [note.number for note in first.notes]) == (1, [1])
        with Tenancy.scope(2):
            second = await anext(reader)
            assert (second.number, [note.number for note in second.notes]) == (3, [3])
            await reader.aclose()
            assert Tenancy.current.get() == 2
            assert [(obj.number, [note.number for note in obj.notes]) for obj in await query] == [
                (2, [2]),
                (4, [4]),
                (6, []),
            ]
    finally:
        await reader.aclose()
