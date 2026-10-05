"""select_for_update(share=True / key_share=True): the shared locks FOR SHARE and FOR KEY SHARE - their
SQL, a run in a transaction, the prefetched rows locked alike - and the flags and databases that refuse
them."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import QueryError, UnSupportedError
from hare.transactions.transactions import Transactions
from tests.testmodels import Event, IntFields, Tournament


@requires_features(supports_select_for_share=True, supports_select_for_key_share=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "lock_sql"),
    [
        ({}, "FOR UPDATE"),
        ({"no_key": True}, "FOR NO KEY UPDATE"),
        ({"share": True}, "FOR SHARE"),
        ({"key_share": True}, "FOR KEY SHARE"),
        ({"share": True, "skip_locked": True}, "FOR SHARE SKIP LOCKED"),
        ({"key_share": True, "nowait": True, "of": ("intfields",)}, 'FOR KEY SHARE OF "intfields" NOWAIT'),
    ],
)
async def test_the_lock_of_each_strength(db, kwargs, lock_sql):
    row = await IntFields.objects.create(intnum=1)
    queryset = IntFields.objects.filter(id=row.id).select_for_update(**kwargs)
    assert queryset.sql().endswith(lock_sql)
    async with Transactions.atomic():
        assert [locked.id for locked in await queryset] == [row.id]


@requires_features(supports_select_for_share=True)
@pytest.mark.asyncio
async def test_the_prefetched_rows_are_locked_alike(db):
    tournament = await Tournament.objects.create(name="cup")
    await Event.objects.create(name="final", tournament=tournament)
    async with Transactions.atomic(), capture_queries() as queries:
        tournaments = (
            await Tournament.objects.filter(id=tournament.id).prefetch_related("events").select_for_update(share=True)
        )
    assert [event.name for event in tournaments[0].events] == ["final"]
    assert len(queries.queries) == 2
    assert all(" FOR SHARE" in sql for sql in queries.queries)


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "feature"), [("share", "supports_select_for_share"), ("key_share", "supports_select_for_key_share")]
)
async def test_a_database_without_the_lock_refuses_it(db, monkeypatch, flag, feature):
    connection = IntFields.get_connection()
    monkeypatch.setattr(connection, "features", connection.features.replace(**{feature: False}))
    async with capture_queries() as queries:
        with pytest.raises(UnSupportedError, match=f"select_for_update\\({flag}=True\\) has no lock"):
            async with Transactions.atomic():
                await IntFields.objects.select_for_update(**{flag: True})
    assert queries.count == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"share": True, "key_share": True}, "takes one of no_key, share and key_share"),
        ({"no_key": True, "share": True}, "takes one of no_key, share and key_share"),
        ({"share": "yes"}, "takes bools for share"),
        ({"nowait": 1}, "takes bools for nowait"),
    ],
)
def test_a_wrong_flag_is_refused(kwargs, message):
    with pytest.raises(QueryError, match=message):
        IntFields.objects.select_for_update(**kwargs)
