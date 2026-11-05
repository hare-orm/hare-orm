"""Prefetch publishes complete results and drains unfinished fetches on every failure."""

import asyncio

import pytest

from hare import prefetch_related_objects
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.prefetching.prefetcher import Prefetcher
from tests.shared_models.testmodels import Event, Team, Tournament


@pytest.mark.asyncio
async def test_attribute_dependencies_keep_source_queries_and_load_required_keys(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="event", tournament=tournament)
    team = await Team.objects.create(name="team")
    await event.participants.add(team)
    source = Event.objects.only("name").order_by("event_id")[:1]
    query = Tournament.objects.prefetch_related(Prefetch("events", source, to_attribute="selected"))
    query = query.prefetch_related(
        "selected__tournament",
        Prefetch("selected__participants", Team.objects.all(), to_attribute="selected_teams"),
    )
    loaded = await query.get(pk=tournament.pk)
    assert len(loaded.selected) == 1
    selected = loaded.selected[0]
    assert selected.tournament.pk == tournament.pk
    assert [obj.pk for obj in selected.selected_teams] == [team.pk]
    assert "_participants" not in vars(selected)
    assert not source._prefetch_map and not source._prefetch_queries
    assert source._fields_for_select == ("name",)
    assert source._limit == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("through_loaded", [False, True])
async def test_successful_sibling_stays_detached_when_another_fetch_fails(db, monkeypatch, through_loaded):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="event", tournament=tournament)
    team = await Team.objects.create(name="team")
    await event.participants.add(team)
    event = await Event.objects.select_related("tournament").get(pk=event.pk)
    fetched = asyncio.Event()
    original = Prefetcher._fetch_node
    successful_field = "tournament" if through_loaded else "participants"

    async def fetch(self, objs, node, request):
        if node.field == successful_field or node.field == "events":
            result = await original(self, objs, node, request)
            if node.field == successful_field:
                fetched.set()
            return result
        await fetched.wait()
        assert "_participants" not in vars(event)
        assert "_events" not in vars(event.tournament)
        raise RuntimeError("related fetch failed")

    monkeypatch.setattr(Prefetcher, "_fetch_node", fetch)
    with pytest.raises(RuntimeError, match="related fetch failed"):
        await prefetch_related_objects([event], "participants", "tournament__events")
    assert "_participants" not in vars(event)
    assert "_events" not in vars(event.tournament)


@pytest.mark.asyncio
@pytest.mark.parametrize("external_cancellation", [False, True])
async def test_unfinished_fetch_is_drained_before_failure_returns(db, monkeypatch, external_cancellation):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="event", tournament=tournament)
    event = await Event.objects.get(pk=event.pk)
    entered = asyncio.Event()
    drained = asyncio.Event()
    never_released = asyncio.Event()

    async def fetch(self, objs, node, request):
        if node.field == "participants":
            try:
                entered.set()
                await never_released.wait()
            finally:
                drained.set()
        else:
            await entered.wait()
            if not external_cancellation:
                raise RuntimeError("related fetch failed")
            await never_released.wait()

    monkeypatch.setattr(Prefetcher, "_fetch_node", fetch)
    task = asyncio.create_task(prefetch_related_objects([event], "participants", "tournament"))
    await entered.wait()
    if external_cancellation:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if external_cancellation else RuntimeError):
        await task
    assert drained.is_set()
    assert "_participants" not in vars(event)
    assert "_tournament" not in vars(event)
