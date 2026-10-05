"""The native layout of prefetched rows (``rust.native.rows``) gives every relation the same rows,
in the same order, as the pure-Python one."""

from unittest.mock import patch

import pytest

from hare.contrib.test import capture_queries
from hare.query.relation_loading.constants import PREFETCH_OWNER_KEY_ANNOTATION
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from tests.testmodels import Event, Team, Tournament


async def make_rows() -> None:
    teams = [await Team.objects.create(id=index, name=f"team {index}") for index in range(1, 5)]
    for tournament_index in range(1, 4):
        tournament = await Tournament.objects.create(id=tournament_index, name=f"tournament {tournament_index}")
        for event_index in range(tournament_index):
            event = await Event.objects.create(
                event_id=tournament_index * 10 + event_index, name=f"event {event_index}", tournament=tournament
            )
            await event.participants.add(*teams[event_index : event_index + 2])
    # A tournament with no events and an event with no participants.
    await Tournament.objects.create(id=9, name="empty")
    await Event.objects.create(event_id=99, name="alone", tournament_id=9)


async def read_layout() -> list[tuple[int, list[tuple[int, list[int]]], list[int]]]:
    tournaments = await Tournament.objects.order_by("id").prefetch_related(
        Prefetch("events", queryset=Event.objects.order_by("-event_id")), "events__participants"
    )
    return [
        (
            tournament.id,
            [(event.event_id, [team.id for team in event.participants]) for event in tournament.events],
            [event.event_id for event in await tournament.events.all().order_by("event_id")],
        )
        for tournament in tournaments
    ]


@pytest.mark.asyncio
async def test_native_and_python_layouts_match(db):
    await make_rows()
    native_layout = await read_layout()
    with patch.object(HydrateAccelerator, "module", None):
        python_layout = await read_layout()
    assert native_layout == python_layout
    assert native_layout[0] == (1, [(10, [1, 2])], [10])
    assert native_layout[-1] == (9, [(99, [])], [99])


@pytest.mark.asyncio
async def test_a_many_to_many_prefetch_reads_the_related_rows_in_one_query(db):
    await make_rows()
    async with capture_queries() as counter:
        events = await Event.objects.filter(tournament_id=3).order_by("event_id").prefetch_related("participants")
    assert counter.count == (2 if HydrateAccelerator.module is not None else 3)
    assert [[team.id for team in event.participants] for event in events] == [[1, 2], [2, 3], [3, 4]]
    # A team linked to two events is one instance, without the owner key it was read with.
    assert events[0].participants[1] is events[1].participants[0]
    assert not hasattr(events[0].participants[1], PREFETCH_OWNER_KEY_ANNOTATION)


@pytest.mark.asyncio
async def test_a_prefetch_queryset_filtering_by_the_relation_reads_the_through_table_apart(db):
    await make_rows()
    async with capture_queries() as counter:
        events = await (
            Event.objects.filter(tournament_id=3)
            .order_by("event_id")
            .prefetch_related(Prefetch("participants", queryset=Team.objects.filter(events__event_id=30)))
        )
    assert counter.count == 3
    assert [[team.id for team in event.participants] for event in events] == [[1, 2], [2], []]


@pytest.mark.asyncio
async def test_to_attr_takes_the_python_layout(db):
    await make_rows()
    tournaments = await Tournament.objects.order_by("id").prefetch_related(
        Prefetch("events", queryset=Event.objects.order_by("event_id"), to_attribute="ordered_events")
    )
    assert [[event.event_id for event in tournament.ordered_events] for tournament in tournaments] == [
        [10],
        [20, 21],
        [30, 31, 32],
        [99],
    ]
