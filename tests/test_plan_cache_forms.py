"""Query forms a later query runs on the plan of: NULL tests, long ``__in`` lists of any length,
composite key rows, lookups on a to-many relation's own name, ``__iexact``, JSON path filters and
JSON containment - each with other values giving their own rows."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hare.query.plans.statement.statement_plans import StatementPlans
from tests.testmodels import CharFields, CompositePkThing, Event, IntFields, JSONFields, Team, Tournament


async def run_counting_plan_hits(build: Callable[[], Awaitable[Any]]) -> tuple[Any, int]:
    hits = StatementPlans.hits
    result = await build()
    return result, StatementPlans.hits - hits


@pytest.mark.asyncio
async def test_null_tests_run_on_their_plans(db):
    empty = await IntFields.objects.create(intnum=1, intnum_null=None)
    full = await IntFields.objects.create(intnum=2, intnum_null=5)
    for round_index in range(2):
        ids, hits = await run_counting_plan_hits(
            lambda: IntFields.objects.filter(intnum_null__isnull=True).values_list("id", flat=True)
        )
        assert (list(ids), hits) == ([empty.id], round_index)
        ids, hits = await run_counting_plan_hits(
            lambda: IntFields.objects.filter(intnum_null__isnull=False).values_list("id", flat=True)
        )
        assert (list(ids), hits) == ([full.id], round_index)
        assert [row.id for row in await IntFields.objects.filter(intnum_null=None)] == [empty.id]


@pytest.mark.asyncio
async def test_long_in_lists_of_any_length_share_a_plan(db):
    rows = [await IntFields.objects.create(intnum=index) for index in range(60)]
    await IntFields.objects.filter(intnum__in=list(range(25))).count()
    for length in (25, 40, 59):
        count, hits = await run_counting_plan_hits(
            lambda: IntFields.objects.filter(intnum__in=list(range(length))).count()
        )
        assert (count, hits) == (length, 1)
    count, hits = await run_counting_plan_hits(lambda: IntFields.objects.filter(intnum__in=[*range(30), None]).count())
    assert count == 30
    assert await IntFields.objects.filter(intnum__not_in=list(range(5, 60))).count() == 5
    assert len(rows) == 60


@pytest.mark.asyncio
async def test_composite_key_rows_run_on_their_plan(db):
    for thing_id in range(1, 5):
        await CompositePkThing.objects.create(thing_id=thing_id, revision=1, name=f"thing {thing_id}")
    await CompositePkThing.objects.filter(pk__in=[(1, 1), (2, 1)]).count()
    names, hits = await run_counting_plan_hits(
        lambda: CompositePkThing.objects.filter(pk__in=[(3, 1), (4, 2)]).values_list("name", flat=True)
    )
    assert list(names) == ["thing 3"]
    count, hits = await run_counting_plan_hits(
        lambda: CompositePkThing.objects.filter(pk__in=[(3, 1), (4, 1)]).count()
    )
    assert (count, hits) == (2, 1)


@pytest.mark.asyncio
async def test_lookups_on_a_to_many_relation_run_on_their_plan(db):
    first = await Tournament.objects.create(id=1, name="first")
    second = await Tournament.objects.create(id=2, name="second")
    first_event = await Event.objects.create(name="a", tournament=first)
    second_event = await Event.objects.create(name="b", tournament=second)
    red = await Team.objects.create(name="red")
    blue = await Team.objects.create(name="blue")
    await first_event.participants.add(red)
    await second_event.participants.add(blue)
    await Tournament.objects.get(events=first_event)
    await Event.objects.get(participants=red)
    await Event.objects.get(participants__in=[red.pk])
    for event, team, tournament in ((first_event, red, first), (second_event, blue, second)):
        found, hits = await run_counting_plan_hits(lambda: Tournament.objects.get(events=event))
        assert (found.id, hits) == (tournament.id, 1)
        found, hits = await run_counting_plan_hits(lambda: Event.objects.get(participants=team))
        assert (found.pk, hits) == (event.pk, 1)
        found, hits = await run_counting_plan_hits(lambda: Event.objects.get(participants__in=[team.pk]))
        assert (found.pk, hits) == (event.pk, 1)
        names, hits = await run_counting_plan_hits(lambda: event.participants.all().values_list("name", flat=True))
        assert list(names) == [team.name]


@pytest.mark.asyncio
async def test_iexact_runs_on_its_plan(db):
    await CharFields.objects.create(char="Hello")
    await CharFields.objects.create(char="World")
    await CharFields.objects.get(char__iexact="hello")
    for text, expected in (("HELLO", "Hello"), ("world", "World")):
        found, hits = await run_counting_plan_hits(lambda: CharFields.objects.get(char__iexact=text))
        assert (found.char, hits) == (expected, 1)


@pytest.mark.asyncio
async def test_json_filters_run_on_their_plans(db):
    small = await JSONFields.objects.create(data={"a": 1, "b": {"c": "x"}, "tags": ["red"]})
    large = await JSONFields.objects.create(data={"a": 7, "b": {"c": "y"}, "tags": ["blue"]})
    forms = [
        (lambda value: JSONFields.objects.filter(data__a=value), (1, [small.id]), (7, [large.id])),
        (lambda value: JSONFields.objects.filter(data__a__gt=value), (0, [small.id, large.id]), (5, [large.id])),
        (lambda value: JSONFields.objects.filter(data__b__c=value), ("x", [small.id]), ("y", [large.id])),
        (lambda value: JSONFields.objects.filter(data__a__in=value), ([1, 2], [small.id]), ([7, 8], [large.id])),
        (
            lambda value: JSONFields.objects.filter(data__contains=value),
            ({"a": 1}, [small.id]),
            ({"a": 7}, [large.id]),
        ),
        (lambda value: JSONFields.objects.filter(data__has_key=value), ("tags", [small.id, large.id]), ("zz", [])),
    ]
    for build, first, second in forms:
        await build(first[0]).count()
        for value, expected in (first, second):
            ids, hits = await run_counting_plan_hits(
                lambda: build(value).order_by("id").values_list("id", flat=True)  # noqa: B023
            )
            assert list(ids) == expected, value


@pytest.mark.asyncio
async def test_a_forward_relation_filtered_by_a_key_value_runs_on_its_plan(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    for tournament in (first, second):
        await Event.objects.create(name=f"event of {tournament.name}", tournament=tournament)
    await Event.objects.filter(tournament=first.id).values_list("name", flat=True)
    for tournament in (first, second):
        names, hits = await run_counting_plan_hits(
            lambda: Event.objects.filter(tournament=tournament.id).values_list("name", flat=True)  # noqa: B023
        )
        assert (list(names), hits) == ([f"event of {tournament.name}"], 1)
    # An instance and a list keep converting as they did - an instance by its key.
    assert [event.name for event in await Event.objects.filter(tournament=second)] == ["event of second"]
    names = (
        await Event.objects.filter(tournament__in=[first, second.id]).order_by("name").values_list("name", flat=True)
    )
    assert list(names) == ["event of first", "event of second"]
