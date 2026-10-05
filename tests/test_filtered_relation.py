"""FilteredRelation: a relation joined under a name of its own with a condition in its ON clause -
read in filters, F(), aggregates, values() and order_by(), beside the relation's own JOIN, and the
conditions and uses it refuses."""

from __future__ import annotations

import pytest

from hare.exceptions import FieldError, QueryError
from hare.query.expressions import F, FilteredRelation, Q
from hare.query.functions import Count, Length, Max, Upper
from tests.testmodels import Event, Team, Tournament


async def create_tournaments() -> tuple[Tournament, Tournament, Tournament]:
    first = await Tournament.objects.create(id=1, name="First")
    second = await Tournament.objects.create(id=2, name="Second")
    third = await Tournament.objects.create(id=3, name="Third")
    for event_id, (tournament, name) in enumerate(
        [(first, "Alpha"), (first, "Beta"), (first, "Arrow"), (second, "Gamma"), (second, "Atlas")], start=1
    ):
        await Event.objects.create(event_id=event_id, tournament=tournament, name=name)
    return first, second, third


def a_events() -> FilteredRelation:
    return FilteredRelation("events", condition=Q(events__name__startswith="A"))


@pytest.mark.asyncio
async def test_a_filtered_relation_counts_only_the_matching_rows(db):
    await create_tournaments()
    rows = (
        await Tournament.objects.alias(a_events=a_events())
        .annotate(a_count=Count("a_events"))
        .values("name", "a_count")
        .order_by("id")
    )
    assert rows == [{"name": "First", "a_count": 2}, {"name": "Second", "a_count": 1}, {"name": "Third", "a_count": 0}]
    # Beside the relation's own JOIN each count is distinct - the JOINs repeat each other's rows.
    both = (
        await Tournament.objects.alias(a_events=a_events())
        .annotate(a_count=Count("a_events", distinct=True), all_count=Count("events", distinct=True))
        .values("a_count", "all_count")
        .order_by("id")
    )
    assert both == [{"a_count": 2, "all_count": 3}, {"a_count": 1, "all_count": 2}, {"a_count": 0, "all_count": 0}]
    with pytest.raises(QueryError, match="non-distinct aggregate"):
        await (
            Tournament.objects.alias(a_events=a_events())
            .annotate(a_count=Count("a_events"), all_count=Count("events", distinct=True))
            .values("a_count")
        )


@pytest.mark.asyncio
async def test_a_filtered_relation_in_values_and_order_by_is_a_left_join(db):
    await create_tournaments()
    rows = (
        await Tournament.objects.annotate(a_events=a_events())
        .values("name", "a_events__name")
        .order_by("id", F("a_events__name").asc(nulls_last=True))
    )
    assert rows == [
        {"name": "First", "a_events__name": "Alpha"},
        {"name": "First", "a_events__name": "Arrow"},
        {"name": "Second", "a_events__name": "Atlas"},
        {"name": "Third", "a_events__name": None},
    ]


@pytest.mark.asyncio
async def test_a_filter_through_a_filtered_relation(db):
    await create_tournaments()
    having_a = (
        await Tournament.objects.alias(a_events=a_events()).filter(a_events__isnull=False).distinct().order_by("id")
    )
    assert [tournament.name for tournament in having_a] == ["First", "Second"]
    with_arrow = await Tournament.objects.alias(a_events=a_events()).filter(a_events__name="Arrow")
    assert [tournament.name for tournament in with_arrow] == ["First"]
    # Beta is an event of First, but not one the condition keeps.
    assert await Tournament.objects.alias(a_events=a_events()).filter(a_events__name="Beta").count() == 0


@pytest.mark.asyncio
async def test_the_relation_keeps_its_own_join_beside_the_filtered_one(db):
    await create_tournaments()
    rows = (
        await Tournament.objects.alias(a_events=a_events())
        .filter(events__name="Beta")
        .values("name", "a_events__name")
        .order_by("a_events__name")
    )
    assert rows == [{"name": "First", "a_events__name": "Alpha"}, {"name": "First", "a_events__name": "Arrow"}]


@pytest.mark.asyncio
async def test_a_condition_comparing_the_relations_own_fields(db):
    await create_tournaments()
    same_id = FilteredRelation(
        "events", condition=Q(events__event_id__lte=F("events__event_id")) & Q(events__name="Gamma")
    )
    rows = await Tournament.objects.alias(gamma=same_id).values("name", "gamma__event_id").order_by("id")
    assert [row["gamma__event_id"] for row in rows] == [None, 4, None]


@pytest.mark.asyncio
async def test_a_forward_relation_filtered(db):
    await create_tournaments()
    first_only = FilteredRelation("tournament", condition=Q(tournament__name="First"))
    rows = (
        await Event.objects.alias(first_tournament=first_only)
        .values("name", "first_tournament__name")
        .order_by("event_id")
    )
    assert [row["first_tournament__name"] for row in rows] == ["First", "First", "First", None, None]


async def create_participants() -> None:
    first, second, __ = await create_tournaments()
    red = await Team.objects.create(id=1, name="Red")
    blue = await Team.objects.create(id=2, name="Blue")
    alpha = await Event.objects.get(name="Alpha")
    gamma = await Event.objects.get(name="Gamma")
    await alpha.participants.add(red, blue)
    await gamma.participants.add(blue)


@pytest.mark.asyncio
async def test_a_many_to_many_relation_filtered(db):
    await create_participants()
    blue_teams = FilteredRelation("participants", condition=Q(participants__name="Blue"))
    rows = (
        await Event.objects.alias(blue=blue_teams)
        .filter(event_id__in=[1, 2, 4])
        .values("name", "blue__name")
        .order_by("event_id")
    )
    assert rows == [
        {"name": "Alpha", "blue__name": "Blue"},
        {"name": "Beta", "blue__name": None},
        {"name": "Gamma", "blue__name": "Blue"},
    ]


@pytest.mark.asyncio
async def test_a_relation_path_of_several_hops_and_a_path_beyond_it(db):
    await create_participants()
    red_participants = FilteredRelation("events__participants", condition=Q(events__participants__name="Red"))
    rows = await Tournament.objects.alias(red=red_participants).filter(red__isnull=False).values("name", "red__id")
    assert rows == [{"name": "First", "red__id": 1}]
    a_event_teams = (
        await Tournament.objects.alias(a_events=a_events())
        .filter(a_events__participants__name="Blue")
        .values_list("name", flat=True)
    )
    # Gamma has Blue but isn't an A event; Alpha is.
    assert a_event_teams == ["First"]


@pytest.mark.asyncio
async def test_exclude_or_and_a_repeated_query(db):
    await create_tournaments()
    queryset = Tournament.objects.alias(a_events=a_events()).exclude(a_events__isnull=False).order_by("id")
    assert [tournament.name for tournament in await queryset] == ["Third"]
    assert [tournament.name for tournament in await queryset] == ["Third"]
    either = (
        await Tournament.objects.alias(a_events=a_events())
        .filter(Q(a_events__name="Atlas") | Q(name="Third"))
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert either == ["Second", "Third"]


@pytest.mark.asyncio
async def test_a_condition_through_a_further_relation_is_refused(db):
    await create_participants()
    deeper = FilteredRelation("events", condition=Q(events__participants__name="Blue"))
    with pytest.raises(QueryError, match="only supports direct fields of the related model"):
        await Tournament.objects.alias(deeper=deeper).values("deeper__name")


@pytest.mark.asyncio
async def test_a_function_reads_a_path_through_it_by_name(db):
    await create_tournaments()
    rows = (
        await Tournament.objects.alias(a_events=a_events())
        .annotate(loud=Upper("a_events__name"), longest=Max(Length("a_events__name")))
        .values("name", "longest")
        .order_by("id")
    )
    assert rows == [
        {"name": "First", "longest": 5},
        {"name": "Second", "longest": 5},
        {"name": "Third", "longest": None},
    ]
    louder = (
        await Tournament.objects.alias(a_events=a_events())
        .annotate(loud=Upper("a_events__name"))
        .filter(id=2)
        .values_list("loud", flat=True)
    )
    assert louder == ["ATLAS"]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: FilteredRelation(""), "takes a relation name"),
        (lambda: FilteredRelation("events", condition={"name": "A"}), "takes a Q"),
    ],
)
def test_a_wrong_filtered_relation_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()


@pytest.mark.asyncio
async def test_a_condition_outside_the_relation_or_a_missing_relation_is_refused(db):
    await create_tournaments()
    outside = FilteredRelation("events", condition=Q(name="First"))
    with pytest.raises(QueryError, match="must start with 'events__'"):
        await Tournament.objects.alias(outside=outside).values("outside__name")
    with pytest.raises(FieldError, match="'name' isn't a relation of Tournament"):
        await Tournament.objects.alias(wrong=FilteredRelation("name")).values("wrong__id")
