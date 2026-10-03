import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields, prefetch_related_objects
from hare.exceptions import (
    FieldError,
    NoValuesFetched,
    QueryError,
    UnSupportedError,
)
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.models import Model
from hare.query.functions import Count
from hare.query.relation_loading.prefetch import Prefetch
from tests.testmodels import (
    Address,
    Author,
    Book,
    Event,
    Group,
    Membership,
    MinRelation,
    O2oPkModelWithM2m,
    Person,
    Reporter,
    Team,
    Tournament,
)
from tests.utils.multi_database_context import MultiDatabaseTestContext

FK_ROUTER_MODULE_NAME = "tests._prefetch_fk_router_models"
M2M_ROUTER_MODULE_NAME = "tests._prefetch_m2m_router_models"


@pytest.mark.asyncio
async def test_prefetch(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Event.objects.create(name="Second", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    tournament = await Tournament.objects.all().prefetch_related("events__participants").first()
    assert len(tournament.events[0].participants) == 2
    assert len(tournament.events[1].participants) == 0


@pytest.mark.asyncio
async def test_prefetch_object(db):
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament)
    await Event.objects.create(name="Second", tournament=tournament)
    tournament_with_filtered = (
        await Tournament.objects.all()
        .prefetch_related(Prefetch("events", queryset=Event.objects.filter(name="First")))
        .first()
    )
    tournament = await Tournament.objects.first().prefetch_related("events")
    assert len(tournament_with_filtered.events) == 1
    assert len(tournament.events) == 2


@pytest.mark.asyncio
async def test_prefetch_unknown_field(db):
    with pytest.raises(FieldError, match="Relation events1 for models.Tournament not found"):
        tournament = await Tournament.objects.create(name="tournament")
        await Event.objects.create(name="First", tournament=tournament)
        await Event.objects.create(name="Second", tournament=tournament)
        await (
            Tournament.objects.all()
            .prefetch_related(Prefetch("events1", queryset=Event.objects.filter(name="First")))
            .first()
        )


@pytest.mark.asyncio
async def test_prefetch_m2m(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    fetched_events = (
        await Event.objects.all()
        .prefetch_related(Prefetch("participants", queryset=Team.objects.filter(name="1")))
        .first()
    )
    assert len(fetched_events.participants) == 1


@pytest.mark.asyncio
async def test_prefetch_o2o(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event)

    fetched_events = await Event.objects.all().prefetch_related("address").first()

    assert fetched_events.address.city == "Santa Monica"


@pytest.mark.asyncio
async def test_prefetch_nested(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Event.objects.create(name="Second", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    fetched_tournaments = (
        await Tournament.objects.all()
        .prefetch_related(
            Prefetch("events", queryset=Event.objects.filter(name="First")),
            Prefetch("events__participants", queryset=Team.objects.filter(name="1")),
        )
        .first()
    )
    assert len(fetched_tournaments.events[0].participants) == 1


@pytest.mark.asyncio
async def test_prefetch_nested_with_aggregation(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Event.objects.create(name="Second", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    fetched_tournaments = (
        await Tournament.objects.all()
        .prefetch_related(
            Prefetch("events", queryset=Event.objects.annotate(teams=Count("participants")).filter(teams=2))
        )
        .first()
    )
    assert len(fetched_tournaments.events) == 1
    assert fetched_tournaments.events[0].pk == event.pk


@pytest.mark.asyncio
async def test_prefetch_to_attr_with_nested_prefetch_through_same_relation(db):
    """A nested prefetch through the to_attr of a Prefetch ("custom_events__address") loads into
    that attribute's instances, once - no second query for the relation, the nested data
    attached for every parent instance."""
    tournament_one = await Tournament.objects.create(name="tournament one")
    tournament_two = await Tournament.objects.create(name="tournament two")
    event_one = await Event.objects.create(name="First", tournament=tournament_one)
    event_two = await Event.objects.create(name="Second", tournament=tournament_two)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event_one)
    await Address.objects.create(city="Boston", street="Beacon", event=event_two)

    executed_sql: list[str] = []

    def hook(event):
        if event.sql:
            executed_sql.append(event.sql)

    Observers.observe(QueryExecuted, hook)
    try:
        tournaments = (
            await Tournament.objects.all()
            .prefetch_related(
                Prefetch("custom_events__address", Address.objects.all()),
                Prefetch("events", Event.objects.all(), to_attr="custom_events"),
            )
            .order_by("name")
        )
        # Hooks now dispatch in the background (never awaited by the query call itself) - drain
        # every in-flight one before asserting on their effect, or executed_sql stays empty and
        # the duplicate-query check below passes vacuously instead of meaning anything.
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert executed_sql, "the hook never recorded any query - the check below would be vacuous"
    assert len(executed_sql) == len(set(executed_sql)), f"duplicate query detected: {executed_sql}"

    tournaments_by_name = {tournament.name: tournament for tournament in tournaments}
    assert tournaments_by_name["tournament one"].custom_events[0].address.city == "Santa Monica"
    assert tournaments_by_name["tournament two"].custom_events[0].address.city == "Boston"


@pytest.mark.asyncio
async def test_nested_prefetch_beside_a_to_attr_prefetch_goes_through_the_plain_relation(db):
    """A nested path through a relation ("events__address") loads the plain relation, like
    Django - a Prefetch of the same relation with to_attr= loads its own attribute only."""
    tournament = await Tournament.objects.create(name="tournament")
    first_event = await Event.objects.create(name="First", tournament=tournament)
    await Event.objects.create(name="Second", tournament=tournament)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=first_event)

    fetched = await Tournament.objects.get(id=tournament.id).prefetch_related(
        "events__address", Prefetch("events", Event.objects.filter(name="Second"), to_attr="second_events")
    )

    assert sorted(event.name for event in fetched.events) == ["First", "Second"]
    assert {event.name: event.address.city if event.address else None for event in fetched.events} == {
        "First": "Santa Monica",
        "Second": None,
    }
    assert [event.name for event in fetched.second_events] == ["Second"]
    assert "address" not in fetched.second_events[0].__dict__ and "_address" not in fetched.second_events[0].__dict__


@pytest.mark.asyncio
async def test_prefetch_through_a_to_attr_given_in_another_call(db):
    """A path through a to_attr may follow the Prefetch in a later prefetch_related() call, and an
    unknown first name still raises."""
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Address.objects.create(city="Boston", street="Beacon", event=event)

    fetched = (
        await Tournament.objects.filter(id=tournament.id)
        .prefetch_related(Prefetch("events", Event.objects.all(), to_attr="all_events"))
        .prefetch_related("all_events__address")
    )

    assert fetched[0].all_events[0].address.city == "Boston"
    with pytest.raises(FieldError, match="Relation missing_events for models.Tournament not found"):
        Tournament.objects.all().prefetch_related("missing_events__address")


@pytest.mark.asyncio
async def test_bare_prefetch_alongside_a_to_attr_prefetch_on_the_same_relation(db):
    """Prefetch(relation, to_attr=X) and a bare "relation" string target DIFFERENT attributes
    (X and the relation's own plain name) - the bare request used to be silently dropped
    whenever the same relation already had a to_attr'd Prefetch, since the code treated "this
    relation already has an entry in prefetch_queries" as proof the bare access was already
    covered, when none of those entries used to_attr=None at all."""
    from tests.testmodels import Employee

    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Alice", manager=boss)
    await Employee.objects.create(name="Bob", manager=boss)

    fresh_boss = (
        await Employee.objects.filter(pk=boss.pk)
        .prefetch_related(
            Prefetch("team_members", Employee.objects.filter(name__startswith="A"), to_attr="a_team"),
            "team_members",
        )
        .first()
    )

    assert fresh_boss is not None
    assert [e.name for e in fresh_boss.a_team] == ["Alice"]
    assert sorted(e.name for e in fresh_boss.team_members) == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_prefetch_direct_relation(db):
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament)
    event = await Event.objects.first().prefetch_related("tournament")
    assert event.tournament.id == tournament.id


@pytest.mark.asyncio
async def test_prefetch_bad_key(db):
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament)
    with pytest.raises(FieldError, match="Relation tour1nament for models.Event not found"):
        await Event.objects.first().prefetch_related("tour1nament")


@pytest.mark.asyncio
async def test_prefetch_m2m_filter(db):
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(team, team_second)
    event = await Event.objects.first().prefetch_related(Prefetch("participants", Team.objects.filter(name="2")))
    assert len(event.participants) == 1
    assert list(event.participants) == [team_second]


@pytest.mark.asyncio
async def test_prefetch_m2m_to_attr(db):
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(team, team_second)
    event = await Event.objects.first().prefetch_related(
        Prefetch("participants", Team.objects.filter(name="1"), to_attr="to_attr_participants_1"),
        Prefetch("participants", Team.objects.filter(name="2"), to_attr="to_attr_participants_2"),
    )
    assert list(event.to_attr_participants_1) == [team]
    assert list(event.to_attr_participants_2) == [team_second]


@pytest.mark.asyncio
async def test_prefetch_m2m_to_attr_does_not_corrupt_bare_relation(db):
    """Two Prefetch(relation, to_attr=X) calls on the SAME relation share one underlying
    ManyToManyRelation container object (cached per instance/relation) - each call's own to_attr
    result is correct and independent (see test_prefetch_m2m_to_attr above), but naively also
    overwriting the shared container's bare related_objects/_fetched on every call used to leave
    the bare relation holding whichever call's own (unrelated) subset happened to run last in
    asyncio.gather(), instead of leaving it correctly unfetched (nobody asked for the bare
    relation - only two distinct to_attr views of it)."""
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(team, team_second)
    event = await Event.objects.first().prefetch_related(
        Prefetch("participants", Team.objects.filter(name="1"), to_attr="to_attr_participants_1"),
        Prefetch("participants", Team.objects.filter(name="2"), to_attr="to_attr_participants_2"),
    )
    with pytest.raises(NoValuesFetched):
        list(event.participants)


@pytest.mark.asyncio
async def test_prefetch_reverse_fk_to_attr_does_not_corrupt_bare_relation(db):
    """Sibling of test_prefetch_m2m_to_attr_does_not_corrupt_bare_relation for a reverse FK."""
    tournament = await Tournament.objects.create(name="tournament")
    for i in range(5):
        await Event.objects.create(name=f"e{i}", tournament=tournament)

    tournament = (
        await Tournament.objects.filter(pk=tournament.pk)
        .prefetch_related(
            Prefetch("events", queryset=Event.objects.filter(name__in=["e0", "e1"]), to_attr="early_events"),
            Prefetch("events", queryset=Event.objects.filter(name__in=["e3", "e4"]), to_attr="late_events"),
        )
        .first()
    )
    assert sorted(e.name for e in tournament.early_events) == ["e0", "e1"]
    assert sorted(e.name for e in tournament.late_events) == ["e3", "e4"]
    with pytest.raises(NoValuesFetched):
        list(tournament.events)


@pytest.mark.asyncio
async def test_prefetch_m2m_annotate(db):
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(team)
    event = await Event.objects.first().prefetch_related(
        Prefetch("participants", Team.objects.annotate(count_events=Count("events")))
    )
    for team in event.participants:
        assert team.count_events == 1


@pytest.mark.asyncio
async def test_prefetch_m2m_select_related(db):
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    event = await Event.objects.create(name="First", tournament=tournament)
    await team.events.add(event)
    team = await Team.objects.first().prefetch_related(
        Prefetch("events", Event.objects.all().select_related("tournament"))
    )
    for event in team.events:
        assert event.tournament == tournament


@pytest.mark.asyncio
async def test_prefetch_m2m_order_by(db):
    tournament = await Tournament.objects.create(name="tournament")
    team_1 = await Team.objects.create(name="1")
    team_2 = await Team.objects.create(name="2")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(team_1, team_2)
    event_1 = await Event.objects.first().prefetch_related(
        Prefetch("participants", Team.objects.all().order_by("name"))
    )
    event_2 = await Event.objects.first().prefetch_related(
        Prefetch("participants", Team.objects.all().order_by("-name"))
    )
    assert [team.name for team in event_1.participants] == ["1", "2"]
    assert [team.name for team in event_2.participants] == ["2", "1"]


@pytest.mark.asyncio
async def test_prefetch_m2m_only(db):
    tournament = await Tournament.objects.create(name="tournament")
    team = await Team.objects.create(name="1")
    event = await Event.objects.create(name="First", tournament=tournament)
    await team.events.add(event)
    team = await Team.objects.first().prefetch_related(Prefetch("events", Event.objects.all().only("name")))
    assert len(team.events) == 1
    for event in team.events:
        assert bool(event.pk)


@pytest.mark.asyncio
async def test_prefetch_o2o_to_attr(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    address = await Address.objects.create(city="Santa Monica", street="Ocean", event=event)
    event = await Event.objects.get(pk=event.pk).prefetch_related(
        Prefetch("address", to_attr="to_address", queryset=Address.objects.all())
    )
    assert address.pk == event.to_address.pk


@pytest.mark.asyncio
async def test_prefetch_o2o_to_attr_when_relation_is_null(db):
    """Sibling of test_prefetch_direct_relation_to_attr_when_relation_is_null for a reverse O2O -
    no Address row at all for this event."""
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    event = await Event.objects.get(pk=event.pk).prefetch_related(
        Prefetch("address", to_attr="to_address", queryset=Address.objects.all())
    )
    assert event.to_address is None


@pytest.mark.asyncio
async def test_prefetch_o2o_to_attr_does_not_corrupt_bare_relation(db):
    """Sibling of test_prefetch_m2m_to_attr_does_not_corrupt_bare_relation for a reverse O2O."""
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    address = await Address.objects.create(city="Santa Monica", street="Ocean", event=event)

    event = await Event.objects.get(pk=event.pk).prefetch_related(
        Prefetch("address", to_attr="a", queryset=Address.objects.filter(city="Santa Monica")),
        Prefetch("address", to_attr="b", queryset=Address.objects.filter(city="Nowhere")),
    )
    assert event.a.pk == address.pk
    assert event.b is None
    # Left uncached rather than corrupted with either to_attr call's own result - falls back to
    # a fresh lazy fetch and correctly resolves the real, actual address, not a stale/wrong one.
    bare_address = await event.address
    assert bare_address.pk == address.pk


@pytest.mark.asyncio
async def test_prefetch_direct_relation_to_attr(db):
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament)
    event = await Event.objects.first().prefetch_related(
        Prefetch("tournament", queryset=Tournament.objects.all(), to_attr="to_attr_tournament")
    )
    assert event.to_attr_tournament.id == tournament.id


@pytest.mark.asyncio
async def test_prefetch_direct_relation_to_attr_when_relation_is_null(db):
    """A to_attr Prefetch never reached the per-row assignment loop at all for an instance whose
    shadow FK column is null (no related row to fetch) - to_attr itself was never set on the
    instance either, so accessing it raised a bare AttributeError instead of correctly returning
    None for "no related row at all"."""
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament, reporter=None)
    event = await Event.objects.first().prefetch_related(
        Prefetch("reporter", queryset=Reporter.objects.all(), to_attr="to_attr_reporter")
    )
    assert event.to_attr_reporter is None


@pytest.mark.asyncio
async def test_prefetch_direct_relation_to_attr_does_not_corrupt_bare_relation(db):
    """Sibling of test_prefetch_m2m_to_attr_does_not_corrupt_bare_relation for a direct FK."""
    tournament_one = await Tournament.objects.create(name="one")
    await Tournament.objects.create(name="two")
    await Event.objects.create(name="First", tournament=tournament_one)

    event = await Event.objects.first().prefetch_related(
        Prefetch("tournament", queryset=Tournament.objects.filter(name="one"), to_attr="a"),
        Prefetch("tournament", queryset=Tournament.objects.filter(name="two"), to_attr="b"),
    )
    assert event.a.name == "one"
    assert event.b is None
    # Left uncached rather than corrupted with either to_attr call's own result - falls back to
    # a fresh lazy fetch (the FK's own getter already handles an uncached access this way) and
    # correctly resolves the real, actual relation, not a stale/wrong one.
    bare_tournament = await event.tournament
    assert bare_tournament.name == "one"


@pytest.mark.asyncio
async def test_prefetch_reverse_fk_only(db):
    tournament = await Tournament.objects.create(name="tournament")
    await Event.objects.create(name="First", tournament=tournament)
    tournament = await Tournament.objects.first().prefetch_related(
        Prefetch("events", queryset=Event.objects.all().only("name"))
    )
    assert len(tournament.events) == 1
    assert tournament.events[0].name == "First"


@pytest.mark.asyncio
async def test_prefetch_reverse_o2o_only(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event)
    event = await Event.objects.get(pk=event.pk).prefetch_related(
        Prefetch("address", queryset=Address.objects.all().only("city"))
    )
    assert event.address.city == "Santa Monica"


@pytest.mark.asyncio
async def test_prefetch_direct_fk_only(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    event = await Event.objects.get(pk=event.pk).prefetch_related(
        Prefetch("tournament", queryset=Tournament.objects.all().only("name"))
    )
    assert event.tournament.name == "tournament"


@pytest.mark.asyncio
async def test_prefetch_reverse_relation_rejects_queryset_limit(db):
    """Prefetch("events", queryset=...) with its own .limit()/slice used to apply that LIMIT to
    the SINGLE combined query every reverse-FK prefetch issues for ALL parent tournaments at once
    - live: 3 tournaments x 3 events with a `[:1]` queryset gave tournament 1 one event and the
    other two tournaments NO events at all (1 prefetched row total instead of 3), silently wrong
    rather than raising. Rejected outright now instead - there's no correct single-query rewrite
    of a per-parent LIMIT without a window-function-ranked subquery."""
    tournament_1 = await Tournament.objects.create(name="T1")
    tournament_2 = await Tournament.objects.create(name="T2")
    for tournament in (tournament_1, tournament_2):
        await Event.objects.create(name="A", tournament=tournament)
        await Event.objects.create(name="B", tournament=tournament)

    with pytest.raises(UnSupportedError):
        await Tournament.objects.all().prefetch_related(
            Prefetch("events", queryset=Event.objects.all().order_by("event_id")[:1])
        )

    with pytest.raises(UnSupportedError):
        await Tournament.objects.all().prefetch_related(Prefetch("events", queryset=Event.objects.all().offset(1)))


@pytest.mark.asyncio
async def test_prefetch_m2m_rejects_queryset_limit(db):
    """Same per-parent-vs-combined-query bug as the reverse-FK case above, through an M2M
    relation instead of a plain FK - _prefetch_m2m_relation's own related_queryset.filter(pk__in=
    ...) is exactly as vulnerable to a caller-supplied LIMIT/OFFSET bounding the combined result
    instead of each parent's own group."""
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team_1 = await Team.objects.create(name="1")
    team_2 = await Team.objects.create(name="2")
    await event.participants.add(team_1, team_2)

    with pytest.raises(UnSupportedError):
        await Event.objects.all().prefetch_related(Prefetch("participants", queryset=Team.objects.all().limit(1)))


@pytest.mark.asyncio
async def test_prefetch_m2m_rejects_through_model_queryset(db):
    """Prefetch(field, queryset=...) on a many-to-many relation must be built from the relation's
    own related model - passing the THROUGH model's queryset instead (e.g. Membership, not Group,
    for Person.groups) used to silently match nothing at all: _prefetch_m2m_relation read
    related_queryset.model._meta as if it were the real target, so it filtered/matched by the
    through model's own pk shape instead of Group's, turning a real relation into an empty result
    with no exception."""
    person = await Person.objects.create(name="Alice")
    group = await Group.objects.create(name="Admins")
    await person.groups.add(group)

    with pytest.raises(QueryError):
        await Person.objects.all().prefetch_related(Prefetch("groups", queryset=Membership.objects.filter())).first()

    # The correct usage - queryset built from the real related model - still works.
    fetched = (
        await Person.objects.all()
        .prefetch_related(Prefetch("groups", queryset=Group.objects.filter(name="Admins")))
        .first()
    )
    assert len(fetched.groups) == 1
    assert fetched.groups[0].name == "Admins"


@pytest.mark.asyncio
async def test_prefetch_queries_isolated_across_clones(db):
    """A clone's own prefetch_related(Prefetch(...)) call must not silently also append to a
    sibling clone's (or the shared base's) prefetch list for the same relation - each clone
    only gets what it itself asked for."""
    base_query = Event.objects.all().prefetch_related(
        Prefetch("tournament", queryset=Tournament.objects.all(), to_attr="a1")
    )
    branch1 = base_query.prefetch_related(Prefetch("tournament", queryset=Tournament.objects.all(), to_attr="a2"))
    branch2 = base_query.prefetch_related(Prefetch("tournament", queryset=Tournament.objects.all(), to_attr="a3"))

    assert len(base_query._prefetch_queries["tournament"]) == 1
    assert len(branch1._prefetch_queries["tournament"]) == 2
    assert len(branch2._prefetch_queries["tournament"]) == 2
    assert base_query._prefetch_queries["tournament"] is not branch1._prefetch_queries["tournament"]


@pytest.mark.asyncio
async def test_chained_prefetch_related_calls_accumulate_plain_string_relations(db):
    """A second .prefetch_related() call used to unconditionally reset _prefetch_map to {},
    discarding whatever plain-string relations an EARLIER .prefetch_related() call in the same
    chain had already registered - unlike Prefetch(...) objects, which correctly accumulate into
    the separate _prefetch_queries dict. `events` silently ended up unfetched here despite being
    explicitly requested, raising on access."""
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)
    await MinRelation.objects.create(tournament=t1)

    qs = Tournament.objects.all().prefetch_related("events").prefetch_related("minrelations")
    assert qs._prefetch_map.keys() == {"events", "minrelations"}

    tournament = (await qs)[0]
    assert list(tournament.events.related_objects) != []
    assert list(tournament.minrelations.related_objects) != []


@pytest.mark.asyncio
async def test_prefetch_map_nested_relation_isolated_across_clones(db):
    """_clone()'s shallow dict.copy() of _prefetch_map only copies the OUTER dict - each entry's
    set was still the SAME object a clone's own base queryset (or a sibling clone) holds, so
    adding a nested prefetch path on one clone (`.prefetch_related("events__reporter")`) used to
    silently mutate that shared set, retroactively adding "reporter" to the BASE queryset's own
    already-built _prefetch_map["events"] too, even though the base was never asked for it."""
    base_query = Tournament.objects.all().prefetch_related("events")
    child_query = base_query.prefetch_related("events__reporter")

    assert base_query._prefetch_map["events"] == set()
    assert child_query._prefetch_map["events"] == {"reporter"}


@pytest_asyncio.fixture
async def fk_prefetch_router():
    """RoutedAuthor is sent to "other" by a configured ConnectionRouter, RoutedBook stays on
    "default" - covers both directions of a plain string prefetch_related() (no explicit
    Prefetch queryset): direct FK (Book -> author) and reverse FK (Author -> books). Neither
    related query should be forced onto the OTHER side's connection."""

    class RoutedAuthor(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        books: fields.ReverseRelation["RoutedBook"]

        class Meta:
            app = "prefetch_fk_router"

    class RoutedBook(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        author = fields.ForeignKeyField("prefetch_fk_router.RoutedAuthor", related_name="books", db_constraint=False)

        class Meta:
            app = "prefetch_fk_router"

    class AuthorOnlyRouter:
        def db_for_read(self, model):
            return "other" if model is RoutedAuthor else None

        def db_for_write(self, model):
            return "other" if model is RoutedAuthor else None

    module = types.ModuleType(FK_ROUTER_MODULE_NAME)
    setattr(module, "RoutedAuthor", RoutedAuthor)  # noqa: B010
    setattr(module, "RoutedBook", RoutedBook)  # noqa: B010
    sys.modules[FK_ROUTER_MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default", "other"],
            apps={"prefetch_fk_router": {"models": [FK_ROUTER_MODULE_NAME], "default_connection": "default"}},
            routers=[AuthorOnlyRouter],
        ) as ctx:
            await ctx.generate_schemas()
            # Both models' tables are only ever created for their static default connection
            # ("default") - "other" needs the same DDL applied manually to simulate an
            # already-migrated alias, same as what a real router setup assumes is in place.
            default_db = ctx.connections.get("default")
            other_db = ctx.connections.get("other")
            await other_db.execute_script(default_db.get_schema_sql(safe=True))
            yield RoutedAuthor, RoutedBook
    finally:
        sys.modules.pop(FK_ROUTER_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_prefetch_direct_relation_uses_related_models_own_router_connection(fk_prefetch_router):
    """_make_prefetch_queries() used to build the default related query as
    related_model.objects.all().using(self.db) - self.db being the PARENT's own resolved connection,
    forced onto the child regardless of the child's own router decision. RoutedAuthor only really
    exists on "other" (router-routed); a plain prefetch_related("author") from RoutedBook (which
    stays on "default") must still find it there."""
    RoutedAuthor, RoutedBook = fk_prefetch_router
    author = await RoutedAuthor.objects.create(name="Ada")
    book = await RoutedBook.objects.create(title="Notes", author=author)

    fetched = await RoutedBook.objects.filter(id=book.id).prefetch_related("author").first()
    assert fetched.author is not None
    assert fetched.author.id == author.id


@pytest.mark.asyncio
async def test_prefetch_reverse_relation_uses_related_models_own_router_connection(fk_prefetch_router):
    """Same bug, opposite direction: RoutedBook stays on "default" while RoutedAuthor is routed
    to "other" - prefetch_related("books") from the routed-away parent must still resolve the
    child (unrouted) query on its own connection, not the parent's."""
    RoutedAuthor, RoutedBook = fk_prefetch_router
    author = await RoutedAuthor.objects.create(name="Ada")
    await RoutedBook.objects.create(title="Notes", author=author)
    await RoutedBook.objects.create(title="More Notes", author=author)

    fetched = await RoutedAuthor.objects.filter(id=author.id).prefetch_related("books").first()
    assert {book.title for book in fetched.books} == {"Notes", "More Notes"}


@pytest_asyncio.fixture
async def m2m_prefetch_router():
    """RoutedBook forward-declares the m2m field (the through table's owning side, per
    ManyToManyRelation._through_table_db()) and is routed to "other"; RoutedTag stays on
    "default". Covers _prefetch_m2m_relation()'s through-table lookup (must follow the OWNING
    side's router decision, from either side of the relation) and its target-model query (must
    follow the TARGET model's own router decision)."""

    class RoutedTag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        books: fields.ManyToManyRelation["RoutedBook"]

        class Meta:
            app = "prefetch_m2m_router"

    class RoutedBook(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        tags: fields.ManyToManyRelation[RoutedTag] = fields.ManyToManyField(
            "prefetch_m2m_router.RoutedTag", related_name="books", db_constraint=False
        )

        class Meta:
            app = "prefetch_m2m_router"

    class BookOnlyRouter:
        def db_for_read(self, model):
            return "other" if model is RoutedBook else None

        def db_for_write(self, model):
            return "other" if model is RoutedBook else None

    module = types.ModuleType(M2M_ROUTER_MODULE_NAME)
    setattr(module, "RoutedTag", RoutedTag)  # noqa: B010
    setattr(module, "RoutedBook", RoutedBook)  # noqa: B010
    sys.modules[M2M_ROUTER_MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default", "other"],
            apps={"prefetch_m2m_router": {"models": [M2M_ROUTER_MODULE_NAME], "default_connection": "default"}},
            routers=[BookOnlyRouter],
        ) as ctx:
            await ctx.generate_schemas()
            default_db = ctx.connections.get("default")
            other_db = ctx.connections.get("other")
            await other_db.execute_script(default_db.get_schema_sql(safe=True))
            yield RoutedTag, RoutedBook
    finally:
        sys.modules.pop(M2M_ROUTER_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_prefetch_m2m_forward_side_uses_owning_models_router_connection(m2m_prefetch_router):
    """Prefetching from the forward (owning, non-generated) side: the through-table query must
    follow RoutedBook's own router decision ("other"), not be forced onto self.db."""
    RoutedTag, RoutedBook = m2m_prefetch_router
    book = await RoutedBook.objects.create(title="Notes")
    tag = await RoutedTag.objects.create(name="urgent")
    await book.tags.add(tag)

    fetched = await RoutedBook.objects.filter(id=book.id).prefetch_related("tags").first()
    assert {t.id for t in fetched.tags} == {tag.id}


@pytest.mark.asyncio
async def test_prefetch_m2m_backward_side_uses_owning_models_router_connection(m2m_prefetch_router):
    """Prefetching from the auto-generated backward side (RoutedTag.books): the owning model for
    the through table is still RoutedBook (field_object._generated is True here), so the
    through-table query must still resolve via RoutedBook's router decision, not RoutedTag's."""
    RoutedTag, RoutedBook = m2m_prefetch_router
    book = await RoutedBook.objects.create(title="Notes")
    tag = await RoutedTag.objects.create(name="urgent")
    await book.tags.add(tag)

    fetched = await RoutedTag.objects.filter(id=tag.id).prefetch_related("books").first()
    assert {b.id for b in fetched.books} == {book.id}


NO_ROUTER_MODULE_NAME = "tests._prefetch_no_router_models"


@pytest_asyncio.fixture
async def fk_prefetch_no_router():
    """Two independent connections ("default"/"other"), deliberately NO router configured -
    isolates an explicit .using()/.using() call's own propagation into prefetch_related()'s
    second query, independent from the separately-tested router-based routing case above (see
    fk_prefetch_router)."""

    class NoRouterAuthor(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        books: fields.ReverseRelation["NoRouterBook"]

        class Meta:
            app = "prefetch_no_router"

    class NoRouterBook(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        author = fields.ForeignKeyField("prefetch_no_router.NoRouterAuthor", related_name="books", db_constraint=False)

        reviews: fields.ReverseRelation["NoRouterReview"]

        class Meta:
            app = "prefetch_no_router"

    class NoRouterReview(Model):
        id = fields.IntField(primary_key=True)
        text = fields.TextField()
        book = fields.ForeignKeyField("prefetch_no_router.NoRouterBook", related_name="reviews", db_constraint=False)

        class Meta:
            app = "prefetch_no_router"

    module = types.ModuleType(NO_ROUTER_MODULE_NAME)
    setattr(module, "NoRouterAuthor", NoRouterAuthor)  # noqa: B010
    setattr(module, "NoRouterBook", NoRouterBook)  # noqa: B010
    setattr(module, "NoRouterReview", NoRouterReview)  # noqa: B010
    sys.modules[NO_ROUTER_MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default", "other"],
            apps={"prefetch_no_router": {"models": [NO_ROUTER_MODULE_NAME], "default_connection": "default"}},
        ) as ctx:
            await ctx.generate_schemas()
            default_db = ctx.connections.get("default")
            other_db = ctx.connections.get("other")
            await other_db.execute_script(default_db.get_schema_sql(safe=True))
            yield NoRouterAuthor, NoRouterBook, NoRouterReview, other_db
    finally:
        sys.modules.pop(NO_ROUTER_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_prefetch_related_propagates_explicit_using_when_no_router_configured(fk_prefetch_no_router):
    """_make_prefetch_queries() used to always build the related query as a bare
    related_model.objects.all(), with no visibility into whatever connection the PARENT queryset
    explicitly chose via .using()/.using() - with no router configured at all (the router
    case is already covered by test_prefetch_direct_relation_uses_related_models_own_router_
    connection above), the prefetch silently fell back to related_model's own static
    default_connection instead. Confirmed live via two genuinely separate connections holding
    different data: NoRouterAuthor.using("other").prefetch_related("books") used to read the
    books from "default" (empty) even though the author itself correctly came from "other"."""
    NoRouterAuthor, NoRouterBook, _NoRouterReview, other_db = fk_prefetch_no_router

    other_author = await NoRouterAuthor.objects.using(other_db).create(name="Other-Ada")
    await NoRouterBook.objects.using(other_db).create(title="Other-Notes", author=other_author)

    # .using() (not the old model-level all(using=...), which called _apply_db() directly and never marked
    # the connection as EXPLICITLY chosen) is what _make_prefetch_queries() actually reads.
    fetched = await NoRouterAuthor.objects.filter(pk=other_author.pk).using(other_db).prefetch_related("books").first()
    assert fetched.name == "Other-Ada"
    assert [b.title for b in fetched.books] == ["Other-Notes"]


@pytest.mark.asyncio
async def test_prefetch_related_propagates_explicit_using_two_levels_deep(fk_prefetch_no_router):
    """_make_prefetch_queries() only ever set _router_fallback_db on the immediate child query,
    never the child's own _db_explicitly_chosen flag - so a SECOND nesting level
    ("books__reviews") never saw the parent's explicit connection choice at all, since
    QuerySet._execute()'s own recursive db_explicitly_chosen=self._db_explicitly_chosen pass-
    through read False on that child. Confirmed live: one level ("books") correctly read from
    "other", but two levels deep ("reviews") silently fell back to "default" (empty) instead."""
    NoRouterAuthor, NoRouterBook, NoRouterReview, other_db = fk_prefetch_no_router

    other_author = await NoRouterAuthor.objects.using(other_db).create(name="Other-Ada")
    other_book = await NoRouterBook.objects.using(other_db).create(title="Other-Notes", author=other_author)
    await NoRouterReview.objects.using(other_db).create(text="Great book", book=other_book)

    fetched = (
        await NoRouterAuthor.objects.filter(pk=other_author.pk)
        .using(other_db)
        .prefetch_related("books__reviews")
        .first()
    )
    assert fetched.name == "Other-Ada"
    assert [b.title for b in fetched.books] == ["Other-Notes"]
    assert [r.text for r in fetched.books[0].reviews] == ["Great book"]


@pytest.mark.asyncio
async def test_fetch_related_propagates_explicit_using_db(fk_prefetch_no_router):
    """Model.fetch_related(using=...) used to call fetch_for_list() without forwarding
    db_explicitly_chosen, unlike QuerySet.prefetch_related() (see
    test_prefetch_related_propagates_explicit_using_when_no_router_configured above) - so the
    same explicit-connection case that correctly propagates through prefetch_related() silently
    fell back to related_model's own default connection through fetch_related() instead.
    Confirmed live via two genuinely separate connections holding different data:
    instance.fetch_related("books", using=other_db) used to read the books from "default"
    (empty) even though the instance itself was fetched from "other"."""
    NoRouterAuthor, NoRouterBook, _NoRouterReview, other_db = fk_prefetch_no_router

    other_author = await NoRouterAuthor.objects.using(other_db).create(name="Other-Ada")
    await NoRouterBook.objects.using(other_db).create(title="Other-Notes", author=other_author)

    fetched = await NoRouterAuthor.objects.filter(pk=other_author.pk).using(other_db).first()
    await prefetch_related_objects([fetched], "books", using=other_db)

    assert [b.title for b in fetched.books] == ["Other-Notes"]


@pytest.mark.asyncio
async def test_union_prefetch_related_propagates_explicit_using_db_no_router(fk_prefetch_no_router):
    """UnionQuery.prefetch_related() runs through Model.fetch_for_list(using=self._db) (see
    hare/query/queryset/set_operations.py's own _execute()) - pinning the follow-up prefetch query to the
    SAME connection the union's own SQL ran against, exactly like
    test_prefetch_related_propagates_explicit_using_when_no_router_configured already confirms
    for a plain QuerySet.prefetch_related(). No router is configured here, so a fetch_for_list()
    call that forgot to forward db_explicitly_chosen would silently fall back to "default"
    (empty) instead of "other"."""
    NoRouterAuthor, NoRouterBook, _NoRouterReview, other_db = fk_prefetch_no_router

    author_one = await NoRouterAuthor.objects.using(other_db).create(name="Other-Ada")
    author_two = await NoRouterAuthor.objects.using(other_db).create(name="Other-Bob")
    await NoRouterBook.objects.using(other_db).create(title="Ada-Notes", author=author_one)
    await NoRouterBook.objects.using(other_db).create(title="Bob-Notes", author=author_two)

    qs1 = NoRouterAuthor.objects.filter(pk=author_one.pk).using(other_db)
    qs2 = NoRouterAuthor.objects.filter(pk=author_two.pk).using(other_db)

    result = await qs1.union(qs2).prefetch_related("books")

    by_name = {author.name: author for author in result}
    assert [b.title for b in by_name["Other-Ada"].books] == ["Ada-Notes"]
    assert [b.title for b in by_name["Other-Bob"].books] == ["Bob-Notes"]


@pytest.mark.asyncio
async def test_prefetch_of_a_missing_fk_target_keeps_the_fk_column(db):
    """A prefetched FK whose target row doesn't exist (dangling, soft-deleted, other tenant) used
    to go through the FK property setter with None, which also overwrote the instance's own FK
    column - so an unrelated later save() silently persisted a NULL foreign key."""
    from tests.testmodels import Author, BookNoConstraint

    author = await Author.objects.create(name="gone")
    book = await BookNoConstraint.objects.create(name="book", author=author, rating=1.0)
    await Author._meta.db.execute_script(f"DELETE FROM {Author._meta.db_table} WHERE id = {author.pk}")

    fetched = await BookNoConstraint.objects.get(pk=book.pk)
    await prefetch_related_objects([fetched], "author")
    assert fetched.author is None
    assert fetched.author_id == author.pk

    fetched.name = "renamed"
    await fetched.save()
    assert (await BookNoConstraint.objects.get(pk=book.pk)).author_id == author.pk

    prefetched = await BookNoConstraint.objects.filter(pk=book.pk).prefetch_related("author").get()
    assert prefetched.author is None
    assert prefetched.author_id == author.pk


@pytest.mark.asyncio
async def test_prefetch_direct_relation_across_multiple_source_classes(db):
    """_prefetch_direct_relation's multi-source-class branch indexed model_to_field (keyed by
    the SOURCE instance's class - Book, O2oPkModelWithM2m) by the RELATED object's class
    (always Author here) instead - a class that's never actually a key in that dict, crashing
    with KeyError the moment fetch_for_list() was called with instances from more than one
    model class sharing the same relation field name."""
    author1 = await Author.objects.create(name="A1")
    author2 = await Author.objects.create(name="A2")
    book = await Book.objects.create(name="B1", author=author1, rating=4.5)
    o2o = await O2oPkModelWithM2m.objects.create(author=author2)

    await prefetch_related_objects([book, o2o], "author")
    assert book.author.id == author1.id
    assert o2o.author.id == author2.id


@pytest.mark.asyncio
async def test_nested_prefetch_object_on_a_derived_queryset_does_not_leak_into_the_base(db):
    """A nested Prefetch(...) added on a derived queryset leaves the base queryset's prefetch map alone."""
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await event.participants.add(await Team.objects.create(name="team"))
    base = Tournament.objects.all().prefetch_related("events")
    derived = base.prefetch_related(
        Prefetch("events__participants", queryset=Team.objects.filter(name="team"), to_attr="picked_teams")
    )

    base_rows = await base
    derived_rows = await derived

    assert not hasattr(base_rows[0].events[0], "picked_teams")
    assert [team.name for team in derived_rows[0].events[0].picked_teams] == ["team"]


@pytest.mark.asyncio
async def test_prefetch_m2m_batches_through_lookup_by_bind_param_ceiling(db, monkeypatch):
    """The M2M through-table lookup binds one parameter per parent - it's split into several
    queries instead of exceeding the backend's bind-parameter ceiling."""
    from hare.contrib.test import capture_queries
    from hare.query.relation_loading import prefetcher as prefetcher_module

    tournament = await Tournament.objects.create(name="t")
    team = await Team.objects.create(name="team")
    events = [await Event.objects.create(name=f"e{index:02}", tournament=tournament) for index in range(12)]
    for event in events[::2]:
        await event.participants.add(team)
    # Leaves room for five parent keys per through-table query.
    monkeypatch.setattr(
        prefetcher_module,
        "PREFETCH_BIND_PARAMS_HEADROOM",
        Event.get_connection(for_write=False).features.max_bind_parameters - 5,
    )

    async with capture_queries() as counter:
        prefetched_events = await Event.objects.all().prefetch_related("participants")

    through_queries = [query for query in counter.queries if "event_team" in query and "JOIN" not in query.upper()]
    assert len(through_queries) == 3
    assert [[participant.name for participant in event.participants] for event in prefetched_events] == [
        ["team"] if index % 2 == 0 else [] for index in range(12)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relation", "queryset_model", "to_attr"),
    [
        ("tournament", Tournament, "name"),
        ("tournament", Tournament, "reporter"),
        ("tournament", Tournament, "reporter_id"),
        ("tournament", Tournament, "participants"),
        ("participants", Team, "pk"),
        ("participants", Team, "save"),
    ],
)
async def test_prefetch_to_attr_colliding_with_model_attribute_rejected(db, relation, queryset_model, to_attr):
    """A to_attr naming a field/FK/shadow column/relation/attribute of the model would overwrite it
    on every instance (and save() would write the prefetched list back)."""
    with pytest.raises(QueryError, match=f"to_attr='{to_attr}'"):
        await Event.objects.all().prefetch_related(
            Prefetch(relation, queryset=queryset_model.objects.all(), to_attr=to_attr)
        )


@pytest.mark.asyncio
async def test_prefetch_to_attr_colliding_on_nested_relation_rejected(db):
    tournament = await Tournament.objects.create(name="t")
    await Event.objects.create(name="e", tournament=tournament)

    with pytest.raises(QueryError, match="Event.name"):
        await Tournament.objects.all().prefetch_related(
            Prefetch("events__participants", queryset=Team.objects.all(), to_attr="name")
        )


@pytest.mark.asyncio
async def test_fetch_related_to_attr_colliding_with_field_rejected(db):
    tournament = await Tournament.objects.create(name="t")

    with pytest.raises(QueryError, match="Tournament.name"):
        await prefetch_related_objects([tournament], Prefetch("events", queryset=Event.objects.all(), to_attr="name"))
    assert tournament.name == "t"


@pytest.mark.asyncio
async def test_prefetch_with_values_queryset_rejected(db):
    with pytest.raises(QueryError, match=r"needs a model QuerySet, got a \.values\(\) query"):
        Prefetch("events", queryset=Event.objects.all().values("name"))
    with pytest.raises(QueryError, match=r"needs a model QuerySet, got a \.values_list\(\) query"):
        Prefetch("events", queryset=Event.objects.all().values_list("name"))
