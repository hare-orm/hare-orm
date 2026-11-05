"""Read paths and write modes preserve an object's explicit key and computed values."""

from contextlib import AsyncExitStack

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError, NoValuesFetched
from hare.instrumentation import Observers, RowsChanged
from hare.transactions import Transactions
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.writes.loaded_write_models import Book, Shelf


@pytest.mark.asyncio
@pytest.mark.parametrize("read_path", ("get", "iterator", "select_related", "prefetch_related", "reassigned"))
@pytest.mark.parametrize("write_path", ("insert", "ignore", "upsert", "upsert_returning", "copy"))
async def test_loaded_key_survives_bulk_create(contract_database, read_path, write_path):
    features = contract_database.get_connection().features
    if write_path in {"ignore", "upsert", "upsert_returning"} and not features.supports_insert_conflicts:
        pytest.skip("conflict clauses are not supported")
    if write_path == "copy" and not features.supports_copy:
        pytest.skip("COPY is not supported")
    contract_database.register_live_models([Shelf, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Shelf.objects.create(id=1)
    await Book.objects.bulk_create([Book(id=key, amount=key * 10, shelf_id=1) for key in (1, 2, 3)])
    query = Book.objects.filter(id=3)
    if read_path == "iterator":
        obj = [obj async for obj in query.iterator(chunk_size=1)][0]
    elif read_path in {"select_related", "prefetch_related"}:
        obj = await getattr(query, read_path)("shelf").get()
    else:
        obj = await query.get()
    if read_path == "reassigned":
        obj.pk = 40
    target_key = obj.pk
    obj.amount = 99
    options = {}
    if write_path == "ignore":
        options["ignore_conflicts"] = True
    elif write_path.startswith("upsert"):
        options.update(update_fields=["amount"], on_conflict=["id"], returning=write_path == "upsert_returning")
    elif write_path == "copy":
        options["use_copy"] = True
    expected = [(1, 10), (2, 20), (3, 30)]
    raises_duplicate = write_path in {"insert", "copy"} and features.supports_unique_constraints and target_key == 3
    if raises_duplicate:
        with pytest.raises(IntegrityError):
            await Book.objects.bulk_create([obj], **options)
    else:
        await Book.objects.bulk_create([obj], **options)
        if write_path.startswith("upsert") and target_key == 3:
            expected[-1] = (3, 99)
        elif write_path != "ignore" or target_key == 40:
            expected.append((target_key, 99))
        if write_path != "ignore":
            try:
                assert obj.doubled == 198
            except NoValuesFetched:
                assert write_path != "upsert_returning"
    assert obj.pk == target_key
    assert sorted(await Book.objects.values_list("id", "amount")) == sorted(expected)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
@pytest.mark.parametrize("nested", (False, True))
@pytest.mark.parametrize("batch_size", (None, 1))
async def test_caught_bulk_insert_failure_preserves_only_the_callers_rows(contract_database, nested, batch_size):
    contract_database.register_live_models([Shelf, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Shelf.objects.create(id=1)
    await Book.objects.create(id=1, amount=10, shelf_id=1)
    fresh = Book(id=2, amount=20, shelf_id=1)
    duplicate = Book(id=1, amount=99, shelf_id=1)
    reported = []
    Observers.observe(RowsChanged, reported.append, models=[Book])
    async with Transactions.atomic(CONTRACT_CONNECTION):
        await Book.objects.create(id=3, amount=30, shelf_id=1)
        async with AsyncExitStack() as stack:
            if nested:
                await stack.enter_async_context(Transactions.atomic(CONTRACT_CONNECTION))
            with pytest.raises(IntegrityError):
                await Book.objects.bulk_create([fresh, duplicate], batch_size=batch_size)
            assert await Book.objects.order_by("id").values_list("id", "amount") == [(1, 10), (3, 30)]
    assert not fresh._saved_in_db
    assert not duplicate._saved_in_db
    assert [event.pks for event in reported] == [(3,)]
    assert await Book.objects.order_by("id").values_list("id", "amount") == [(1, 10), (3, 30)]
