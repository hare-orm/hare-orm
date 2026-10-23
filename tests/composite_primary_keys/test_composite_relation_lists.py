"""A relation to a composite key compared to a list or a query: ``__in``/``__not_in``/``exclude()`` give
the rows a relation of one column gives - a NULL relation kept by ``not_in`` and ``exclude()``, a NULL
among a query's rows emptying nothing - and a list of thousands of keys is one row-value check, not a
condition thousands of levels deep."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation, hare_test_context
from hare.query.expressions import Q
from tests.composite_primary_keys.composite_key_list_models import CompositeIntKeyEntry, CompositeKeyReference

MODULES = ["tests.composite_primary_keys.composite_key_list_models"]
ENTRY_COUNT = 1500


@pytest_asyncio.fixture(scope="module")
async def references_db_module():
    async with hare_test_context(
        MODULES,
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        entries = [CompositeIntKeyEntry(first=number, second=number % 7) for number in range(ENTRY_COUNT)]
        await CompositeIntKeyEntry.objects.bulk_create(entries)
        await CompositeKeyReference.objects.bulk_create(
            [CompositeKeyReference(id=number, entry=entry) for number, entry in enumerate(entries)]
        )
        await CompositeKeyReference.objects.create(id=ENTRY_COUNT, entry=None)
        yield context, entries


@pytest_asyncio.fixture
async def references_db(references_db_module):
    context, entries = references_db_module
    async with RollbackIsolation(context):
        yield entries


async def get_ids(queryset) -> list[int]:
    return sorted(await queryset.values_list("id", flat=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [3, 1200])
async def test_a_list_of_keys_gives_the_rows_a_relation_of_one_column_gives(references_db, size):
    chosen = references_db[:size]
    chosen_ids = list(range(size))
    other_ids = list(range(size, ENTRY_COUNT))
    keys = [(entry.first, entry.second) for entry in chosen]
    objects = CompositeKeyReference.objects

    assert await get_ids(objects.filter(entry__in=chosen)) == chosen_ids
    assert await get_ids(objects.filter(entry__in=keys)) == chosen_ids
    # The NULL relation isn't in the list.
    assert await get_ids(objects.filter(entry__not_in=chosen)) == [*other_ids, ENTRY_COUNT]
    assert await get_ids(objects.exclude(entry__in=keys)) == [*other_ids, ENTRY_COUNT]
    assert await get_ids(objects.filter(~Q(entry__in=chosen))) == [*other_ids, ENTRY_COUNT]
    assert await get_ids(objects.exclude(entry__not_in=chosen)) == chosen_ids


@pytest.mark.asyncio
async def test_a_null_among_the_rows_of_a_query_empties_nothing(references_db):
    referenced = CompositeKeyReference.objects.filter(id__lt=5).values_list("entry", flat=True)
    with_null = CompositeKeyReference.objects.filter(Q(id__lt=5) | Q(entry=None)).values_list("entry", flat=True)
    entries = CompositeIntKeyEntry.objects

    for query in (referenced, with_null):
        unreferenced_count = await entries.exclude(pk__in=query).count()
        assert unreferenced_count == ENTRY_COUNT - 5
        assert await entries.filter(pk__not_in=query).count() == unreferenced_count
    assert await get_ids(CompositeKeyReference.objects.exclude(entry__in=entries.filter(first__gte=3))) == [
        0,
        1,
        2,
        ENTRY_COUNT,
    ]
