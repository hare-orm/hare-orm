import pytest

from hare.query.expressions import F
from hare.query.functions import Length
from tests.testmodels import Event, Tournament


@pytest.mark.asyncio
async def test_annotations(db):
    a = await Tournament.objects.create(name="A")

    base_query = Tournament.objects.annotate(id_plus_one=F("id") + 1)
    query1 = base_query.annotate(id_plus_two=F("id") + 2)
    query2 = base_query.annotate(id_plus_three=F("id") + 3)
    res = await query1.first()
    assert res.id_plus_one == a.id + 1
    assert res.id_plus_two == a.id + 2
    with pytest.raises(AttributeError):
        getattr(res, "id_plus_three")

    res = await query2.first()
    assert res.id_plus_one == a.id + 1
    assert res.id_plus_three == a.id + 3
    with pytest.raises(AttributeError):
        getattr(res, "id_plus_two")

    res = await query1.first()
    with pytest.raises(AttributeError):
        getattr(res, "id_plus_three")


@pytest.mark.asyncio
async def test_filters(db):
    a = await Tournament.objects.create(name="A")
    b = await Tournament.objects.create(name="B")
    await Tournament.objects.create(name="C")

    base_query = Tournament.objects.exclude(name="C")
    tournaments = await base_query
    assert set(tournaments) == {a, b}

    tournaments = await base_query.exclude(name="A")
    assert set(tournaments) == {b}

    tournaments = await base_query.exclude(name="B")
    assert set(tournaments) == {a}


@pytest.mark.asyncio
async def test_joins(db):
    tournament_a = await Tournament.objects.create(name="A")
    tournament_b = await Tournament.objects.create(name="B")
    tournament_c = await Tournament.objects.create(name="C")
    event_a = await Event.objects.create(name="A", tournament=tournament_a)
    event_b = await Event.objects.create(name="B", tournament=tournament_b)
    await Event.objects.create(name="C", tournament=tournament_c)

    base_query = Event.objects.exclude(tournament__name="C")
    events = await base_query
    assert set(events) == {event_a, event_b}

    events = await base_query.exclude(name="A")
    assert set(events) == {event_b}

    events = await base_query.exclude(name="B")
    assert set(events) == {event_a}


@pytest.mark.asyncio
async def test_select_related_isolated_across_clones(db):
    """.select_related() must not mutate a shared base queryset's own relation set - two
    branches built from the same base must each carry only their own relation, and the base
    itself must stay untouched."""
    base_query = Event.objects.all()
    branch_tournament = base_query.select_related("tournament")
    branch_reporter = base_query.select_related("reporter")

    assert base_query._select_related == set()
    assert branch_tournament._select_related == {"tournament"}
    assert branch_reporter._select_related == {"reporter"}


@pytest.mark.asyncio
async def test_order_by(db):
    a = await Tournament.objects.create(name="A")
    b = await Tournament.objects.create(name="B")

    base_query = Tournament.objects.all().order_by("name")
    tournaments = await base_query
    assert tournaments == [a, b]

    tournaments = await base_query.order_by("-name")
    assert tournaments == [b, a]


@pytest.mark.asyncio
async def test_values_with_annotations(db):
    await Tournament.objects.create(name="Championship")
    await Tournament.objects.create(name="Super Bowl")

    base_query = Tournament.objects.annotate(name_length=Length("name"))
    tournaments = await base_query.values_list("name")
    assert sorted(tournaments) == sorted([("Championship",), ("Super Bowl",)])

    tournaments = await base_query.values_list("name_length")
    assert sorted(tournaments) == sorted([(10,), (12,)])
