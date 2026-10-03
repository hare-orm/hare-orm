import pytest

from hare.contrib.test import assert_num_queries, capture_queries, requires_features
from hare.instrumentation.observers import Observers
from tests.testmodels import Event, Tournament


@pytest.mark.asyncio
async def test_capture_queries_counts_select(db):
    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E", tournament=tournament)

    async with capture_queries() as counter:
        await Event.objects.all()

    assert counter.count == 1
    assert "SELECT" in counter.queries[0].upper()


@pytest.mark.asyncio
async def test_capture_queries_select_related_avoids_n_plus_1(db):
    tournament = await Tournament.objects.create(name="T")
    for i in range(3):
        await Event.objects.create(name=f"E{i}", tournament=tournament)

    async with capture_queries() as counter:
        events = await Event.objects.all().select_related("tournament")
        for event in events:
            _ = event.tournament.name

    assert counter.count == 1


@pytest.mark.asyncio
async def test_capture_queries_detects_lazy_fetch_n_plus_1(db):
    tournament = await Tournament.objects.create(name="T")
    for i in range(3):
        await Event.objects.create(name=f"E{i}", tournament=tournament)

    async with capture_queries() as counter:
        events = await Event.objects.all()
        for event in events:
            related = await event.tournament
            _ = related.name

    # 1 (select events) + 3 (one lazy fetch per event)
    assert counter.count == 4


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_capture_queries_counts_insert_update_delete(db):
    async with capture_queries() as counter:
        tournament = await Tournament.objects.create(name="T")
        tournament.name = "T2"
        await tournament.save()
        await tournament.delete()

    assert counter.count == 3


@pytest.mark.asyncio
async def test_assert_num_queries_passes_on_match(db):
    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E", tournament=tournament)

    async with assert_num_queries(1):
        await Event.objects.all().select_related("tournament")


@pytest.mark.asyncio
async def test_assert_num_queries_fails_on_mismatch(db):
    tournament = await Tournament.objects.create(name="T")
    for i in range(2):
        await Event.objects.create(name=f"E{i}", tournament=tournament)

    with pytest.raises(AssertionError) as exc_info:
        async with assert_num_queries(1):
            events = await Event.objects.all()
            for event in events:
                related = await event.tournament
                _ = related.name

    message = str(exc_info.value)
    assert "Expected 1 quer" in message
    assert "executed 3" in message


@pytest.mark.asyncio
async def test_capture_queries_observes_without_patching_the_client(db):
    """capture_queries() observes QueryExecuted in the current context - the client is never
    patched, and nothing is left observing once the block ends."""
    db_client = db.db()
    observers_before = Observers.context_observers.get()

    async with capture_queries() as counter:
        await Tournament.objects.all()
        assert "execute" not in db_client.__dict__
        assert len(Observers.context_observers.get()) == len(observers_before) + 1

    assert counter.count == 1
    assert Observers.context_observers.get() == observers_before


@pytest.mark.asyncio
async def test_capture_queries_stops_observing_on_exception(db):
    observers_before = Observers.context_observers.get()

    with pytest.raises(ValueError):
        async with capture_queries():
            await Tournament.objects.all()
            raise ValueError("boom")

    assert Observers.context_observers.get() == observers_before


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_capture_queries_counts_queries_inside_a_freshly_opened_top_level_transaction(db_simple):
    """Opening a brand-new Transactions.atomic() block for the first time (no transaction
    already ambient when capture_queries() itself was entered - db_simple, unlike db, doesn't wrap
    the test body in one) rebinds the ambient connection to a freshly constructed transactional
    client object, a different instance than the one capture_queries() originally patched -
    queries issued inside that block used to be completely invisible to the counter, silently
    letting assert_num_queries() pass for a real N+1 issued inside the caller's own top-level
    transaction."""
    from hare.transactions.transactions import Transactions

    try:
        async with capture_queries() as counter:
            async with Transactions.atomic():
                await Tournament.objects.create(name="T1")
                await Tournament.objects.create(name="T2")

        assert counter.count == 2
    finally:
        await Tournament.objects.all().delete()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("transaction_options", [{"read_only": True}, {"statement_timeout": 5}])
async def test_capture_queries_passes_transaction_options_through(db_simple, transaction_options):
    """The patched _in_transaction() must accept the same keyword options as the original -
    read_only=/statement_timeout= used to raise a TypeError inside capture_queries()."""
    from hare.transactions.transactions import Transactions

    async with capture_queries() as counter:
        async with Transactions.atomic(**transaction_options):
            await Tournament.objects.all().count()

    assert [query for query in counter.queries if '"tournament"' in query]


@pytest.mark.asyncio
async def test_capture_queries_counts_dict_returning_queries_once_each(db):
    """values()/aggregate()/annotate().values() go through execute_dicts - each counts
    exactly once, even where one counted client method delegates to another."""
    from hare.query.functions import Count

    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E", tournament=tournament)

    async with capture_queries() as counter:
        await Event.objects.all().values("name")
        await Event.objects.all().aggregate(event_count=Count("event_id"))
        await Tournament.objects.annotate(event_count=Count("events")).group_by("id").values("event_count")

    assert counter.count == 3


@pytest.mark.asyncio
async def test_capture_queries_counts_a_stream_once(db):
    from hare.transactions.transactions import Transactions

    tournament = await Tournament.objects.create(name="T")
    for index in range(3):
        await Event.objects.create(name=f"E{index}", tournament=tournament)
    if not db.db().features.supports_streaming:
        pytest.skip("backend has no server-side streaming")

    async with Transactions.atomic():
        async with capture_queries() as counter:
            streamed_names = [event.name async for event in Event.objects.all().order_by("name").stream()]

    assert streamed_names == ["E0", "E1", "E2"]
    assert counter.count == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_capture_queries_counts_a_copy_bulk_create(db_truncate):
    """bulk_create(use_copy=True) runs through copy(), which used to be left uncounted.
    Not transaction-wrapped: rust_pg's COPY can't run inside a transaction."""
    async with assert_num_queries(1) as counter:
        await Tournament.objects.bulk_create([Tournament(id=1, name="A"), Tournament(id=2, name="B")], use_copy=True)

    assert counter.queries[0].startswith("COPY tournament (")
    assert "name" in counter.queries[0]
    assert await Tournament.objects.all().count() == 2
