"""An instance keeps its to-many relations' fetched rows itself and their relation objects weakly: an
instance whose prefetched relations were read is freed by reference counting alone, a relation object
held elsewhere stays the instance's own, and a relation of rows not fetched stays the same object."""

import copy
import gc
import pickle
import weakref

import pytest

from tests.testmodels import Event, Team, Tournament


@pytest.mark.asyncio
async def test_an_instance_whose_prefetched_relations_were_read_is_freed_without_the_collector(db):
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    team = await Team.objects.create(name="reds")
    await event.participants.add(team)

    loaded = await Tournament.objects.filter(pk=tournament.pk).prefetch_related("events__participants").first()
    assert [loaded_event.name async for loaded_event in loaded.events] == ["final"]
    loaded_event = loaded.events.related_objects[0]
    assert [participant.name async for participant in loaded_event.participants] == ["reds"]
    tournament_reference = weakref.ref(loaded)
    event_reference = weakref.ref(loaded_event)
    gc.disable()
    try:
        del loaded, loaded_event
        assert tournament_reference() is None
        assert event_reference() is None
    finally:
        gc.enable()


@pytest.mark.asyncio
async def test_a_held_relation_object_stays_the_instance_s_own(db):
    tournament = await Tournament.objects.create(name="cup")
    await Event.objects.create(name="final", tournament=tournament)
    loaded = await Tournament.objects.filter(pk=tournament.pk).prefetch_related("events").first()
    events = loaded.events
    assert loaded.events is events
    events._invalidate_local_cache()
    assert not loaded.events._fetched
    assert [event.name for event in await loaded.events.all()] == ["final"]


@pytest.mark.asyncio
async def test_the_relation_of_rows_not_fetched_is_one_object(db):
    tournament = await Tournament.objects.create(name="cup")
    assert tournament.events is tournament.events
    await Event.objects.create(name="final", tournament=tournament)
    assert [event.name for event in await tournament.events.all()] == ["final"]


@pytest.mark.asyncio
async def test_fetched_rows_outlive_their_relation_object(db):
    tournament = await Tournament.objects.create(name="cup")
    await Event.objects.create(name="final", tournament=tournament)
    loaded = await Tournament.objects.filter(pk=tournament.pk).prefetch_related("events").first()
    first_reference = weakref.ref(loaded.events)
    gc.collect()
    assert first_reference() is None
    assert loaded.events._fetched
    assert [event.name for event in loaded.events] == ["final"]


@pytest.mark.asyncio
async def test_a_copy_and_a_pickle_keep_their_rows_apart(db):
    tournament = await Tournament.objects.create(name="cup")
    await Event.objects.create(name="final", tournament=tournament)
    loaded = await Tournament.objects.filter(pk=tournament.pk).prefetch_related("events").first()
    duplicate = copy.copy(loaded)
    assert not duplicate.events._fetched
    assert loaded.events._fetched
    restored = pickle.loads(pickle.dumps(loaded))
    assert restored.events._fetched
    assert [event.name for event in restored.events] == ["final"]
    assert restored.events.instance is restored
