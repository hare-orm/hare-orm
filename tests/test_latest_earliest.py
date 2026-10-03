import pytest
import pytest_asyncio

from hare.exceptions import MultipleObjectsReturned, QueryError
from hare.query.expressions import F
from tests.testmodels import CompositePkThing, Employee, Event, IntFields, Team, Tournament


@pytest_asyncio.fixture
async def latest_earliest_data(db):
    """Fixture to set up test data for latest/earliest tests."""
    tournament = Tournament(name="Tournament 1")
    await tournament.save()

    second_tournament = Tournament(name="Tournament 2")
    await second_tournament.save()

    event_first = Event(name="1", tournament=tournament)
    await event_first.save()
    event_second = Event(name="2", tournament=second_tournament)
    await event_second.save()
    event_third = Event(name="3", tournament=tournament)
    await event_third.save()
    event_forth = Event(name="4", tournament=second_tournament)
    await event_forth.save()


@pytest.mark.asyncio
async def test_latest(latest_earliest_data):
    assert await Event.objects.latest("-name") == await Event.objects.get(name="1")
    assert await Event.objects.latest("name") == await Event.objects.get(name="4")
    assert await Event.objects.latest("-name") == await Event.objects.all().order_by("name").first()
    assert await Event.objects.latest("name") == await Event.objects.all().order_by("-name").first()
    assert await Event.objects.latest("tournament__name", "name") == await Event.objects.get(name="4")
    assert await Event.objects.latest("-tournament__name", "name") == await Event.objects.get(name="3")
    assert await Event.objects.latest("tournament__name", "-name") == await Event.objects.get(name="2")
    assert await Event.objects.latest("-tournament__name", "-name") == await Event.objects.get(name="1")


@pytest.mark.asyncio
async def test_earliest(latest_earliest_data):
    assert await Event.objects.earliest("name") == await Event.objects.get(name="1")
    assert await Event.objects.earliest("-name") == await Event.objects.get(name="4")
    assert await Event.objects.earliest("name") == await Event.objects.all().order_by("name").first()
    assert await Event.objects.earliest("-name") == await Event.objects.all().order_by("-name").first()
    assert await Event.objects.earliest("-tournament__name", "-name") == await Event.objects.get(name="4")
    assert await Event.objects.earliest("tournament__name", "-name") == await Event.objects.get(name="3")
    assert await Event.objects.earliest("-tournament__name", "name") == await Event.objects.get(name="2")
    assert await Event.objects.earliest("tournament__name", "name") == await Event.objects.get(name="1")


# ============================================================================
# NULL-robust latest()/earliest(): a NULL never wins - SQLite (NULL is the smallest value) and
# PostgreSQL (NULL is the largest) used to disagree, and PostgreSQL's latest() returned the NULL row
# instead of the maximum.
# ============================================================================


@pytest_asyncio.fixture
async def scores_data(db):
    """intnum 1..5 with intnum_null 1, NULL, 2, 3, NULL."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)


@pytest.mark.asyncio
async def test_latest_returns_the_maximum_non_null_value(scores_data):
    assert (await IntFields.objects.latest("intnum_null")).intnum == 4
    assert (await IntFields.objects.all().latest("intnum_null")).intnum == 4
    assert (await IntFields.objects.latest("-intnum_null")).intnum == 1


@pytest.mark.asyncio
async def test_earliest_returns_the_minimum_non_null_value(scores_data):
    assert (await IntFields.objects.earliest("intnum_null")).intnum == 1
    assert (await IntFields.objects.all().earliest("intnum_null")).intnum == 1
    assert (await IntFields.objects.earliest("-intnum_null")).intnum == 4


@pytest.mark.asyncio
async def test_latest_earliest_composite_with_duplicate_values(db):
    for number, score in [(1, 3), (2, None), (3, 3), (4, None), (5, 1)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    assert (await IntFields.objects.latest("intnum_null", "intnum")).intnum == 3
    assert (await IntFields.objects.latest("intnum_null", "-intnum")).intnum == 1
    assert (await IntFields.objects.earliest("intnum_null", "intnum")).intnum == 5
    assert (await IntFields.objects.earliest("-intnum_null", "-intnum")).intnum == 3


@pytest.mark.asyncio
async def test_latest_earliest_with_a_single_null_row(db):
    await IntFields.objects.create(intnum=1, intnum_null=7)
    await IntFields.objects.create(intnum=2, intnum_null=None)

    assert (await IntFields.objects.latest("intnum_null")).intnum == 1
    assert (await IntFields.objects.earliest("intnum_null")).intnum == 1


@pytest.mark.asyncio
async def test_latest_earliest_when_every_value_is_null_return_a_null_row(db):
    await IntFields.objects.create(intnum=1, intnum_null=None)
    await IntFields.objects.create(intnum=2, intnum_null=None)

    latest = await IntFields.objects.latest("intnum_null")
    earliest = await IntFields.objects.earliest("intnum_null")
    assert latest is not None
    assert latest.intnum_null is None
    assert earliest is not None
    assert earliest.intnum_null is None
    assert await IntFields.objects.filter(intnum=99).latest("intnum_null") is None


@pytest.mark.asyncio
async def test_latest_earliest_by_a_related_field_skip_the_left_join_nulls(db):
    """Employee.name is not nullable, but ordering by manager__name meets NULL for a row with no manager."""
    await Employee.objects.create(name="CEO")
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker", manager=boss)

    assert (await Employee.objects.latest("manager__name")).name == "Worker"
    assert (await Employee.objects.earliest("manager__name")).name == "Worker"


@pytest.mark.asyncio
async def test_latest_earliest_with_explicit_ordering(scores_data):
    """An explicit Ordering is honored: earliest() uses it as given, latest() reverses it exactly like
    last() does - direction and NULL placement both flip."""
    assert (await IntFields.objects.earliest(F("intnum_null").asc(nulls_first=True), "intnum")).intnum == 2
    assert (await IntFields.objects.earliest(F("intnum_null").desc(nulls_last=True), "intnum")).intnum == 4
    assert (await IntFields.objects.latest(F("intnum_null").asc(nulls_first=True), "intnum")).intnum == 4
    assert (await IntFields.objects.latest(F("intnum_null").asc(nulls_last=True), "-intnum")).intnum == 2


@pytest.mark.asyncio
async def test_latest_earliest_skip_null_placement_for_a_non_nullable_field(db):
    """A NOT NULL column keeps the plain direction - an explicit NULLS LAST would only forbid
    index-ordered scans on it."""
    await IntFields.objects.create(intnum=1)

    for query in (
        IntFields.objects.latest("intnum"),
        IntFields.objects.earliest("intnum"),
        IntFields.objects.latest("pk"),
    ):
        assert "NULLS" not in query.sql()
    assert "NULLS LAST" in IntFields.objects.latest("intnum_null").sql()
    assert "NULLS LAST" in IntFields.objects.earliest("intnum_null").sql()


@pytest.mark.asyncio
async def test_last_latest_earliest_stay_within_the_slice(db):
    """A sliced queryset's last()/latest()/earliest() pick from the slice, not from the whole table."""
    first, second, third, fourth = [await Tournament.objects.create(name=name) for name in ("a", "b", "c", "d")]
    ordered = Tournament.objects.all().order_by("id")

    assert await ordered[0:2].last() == second
    assert await ordered[1:3].last() == third
    assert await ordered[1:].last() == fourth
    assert await ordered[0:2].latest("name") == second
    assert await ordered[2:4].earliest("name") == third
    assert await ordered[0:2][1:].last() == second
    assert await ordered.limit(0).last() is None
    assert await ordered.limit(0).latest("name") is None
    assert await ordered.limit(0).first() is None
    assert await ordered[1:3].first() == second
    assert await ordered[0:2].last().values_list("name", flat=True) == "b"
    assert await ordered[0:2].last().values("name") == {"name": "b"}
    assert await ordered.filter(name__in=["a", "c", "d"])[0:2].last() == third
    assert first.name == "a"


@pytest.mark.asyncio
async def test_get_on_a_sliced_queryset_stays_within_the_slice(db):
    """get()/get_or_none() with no conditions on a sliced queryset read the slice's own rows;
    with conditions they filter, which a slice rejects like Django does."""
    first, second, _third, _fourth = [await Tournament.objects.create(name=name) for name in ("a", "b", "c", "d")]
    ordered = Tournament.objects.all().order_by("id")

    assert await ordered.limit(0).get_or_none() is None
    assert await ordered[1:2].get() == second
    assert await ordered[0:1].get_or_none() == first
    with pytest.raises(MultipleObjectsReturned):
        await ordered[1:3].get()
    for sliced_lookup in (
        lambda: ordered[1:3].get(pk=second.pk),
        lambda: ordered[1:3].get_or_none(pk=first.pk),
        lambda: ordered.limit(0).get_or_none(pk=first.pk),
    ):
        with pytest.raises(QueryError, match="Cannot filter a query once a slice has been taken"):
            sliced_lookup()


@pytest.mark.asyncio
async def test_sliced_last_with_select_related_prefetch_and_annotate(db):
    tournament = await Tournament.objects.create(name="t")
    team = await Team.objects.create(name="team")
    events = [await Event.objects.create(name=name, tournament=tournament) for name in ("e1", "e2", "e3")]
    for event in events:
        await event.participants.add(team)

    last_event = (
        await Event.objects.all()
        .order_by("event_id")
        .select_related("tournament")
        .prefetch_related("participants")
        .annotate(doubled=F("event_id") * 2)
        .filter(participants__name="team")[0:2]
        .last()
    )

    assert last_event == events[1]
    assert last_event.tournament.name == "t"
    assert [participant.name for participant in last_event.participants] == ["team"]
    assert last_event.doubled == events[1].event_id * 2


@pytest.mark.asyncio
async def test_sliced_last_on_a_composite_primary_key_stays_within_the_slice(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    assert (await CompositePkThing.objects.all().order_by("name")[0:2].last()).name == "B"
    assert (await CompositePkThing.objects.all().order_by("name").last()).name == "C"
