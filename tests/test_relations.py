import asyncio
import re

import pytest
import pytest_asyncio

from hare import prefetch_related_objects
from hare.contrib.test import requires_features
from hare.exceptions import FieldError, NoValuesFetched, QueryError
from hare.query.functions import Count, Trim
from tests.testmodels import (
    Address,
    Author,
    BookNoConstraint,
    DocumentRevisionNote,
    DoubleFK,
    Employee,
    Event,
    Extra,
    M2mWithO2oPk,
    Node,
    NullableCodeCapital,
    NullableCodeCity,
    NullableCodeCountry,
    O2oPkAccount,
    O2oPkModelWithM2m,
    O2oPkProfile,
    O2oPkProfileNote,
    Pair,
    ProtectedParentWithCode,
    Reporter,
    Single,
    Team,
    Tournament,
    UUIDFkRelatedNullModel,
    VersionedDocument,
)

# =============================================================================
# TestRelations - uses db fixture (transaction rollback)
# =============================================================================


@pytest.mark.asyncio
async def test_relations(db):
    tournament = Tournament(name="New Tournament")
    await tournament.save()
    await Event(name="Without participants", tournament_id=tournament.id).save()
    event = Event(name="Test", tournament_id=tournament.id)
    await event.save()
    participants = []
    for i in range(2):
        team = Team(name=f"Team {(i + 1)}")
        await team.save()
        participants.append(team)
    await event.participants.add(participants[0], participants[1])
    await event.participants.add(participants[0], participants[1])

    with pytest.raises(NoValuesFetched):
        [team.id for team in event.participants]  # pylint: disable=W0104

    teamids = []
    async for team in event.participants:
        teamids.append(team.id)
    assert set(teamids) == {participants[0].id, participants[1].id}
    teamids = [team.id async for team in event.participants]
    assert set(teamids) == {participants[0].id, participants[1].id}

    assert {team.id for team in event.participants} == {participants[0].id, participants[1].id}

    assert event.participants[0].id in {participants[0].id, participants[1].id}

    selected_events = await Event.objects.filter(participants=participants[0].id).prefetch_related(
        "participants", "tournament"
    )
    assert len(selected_events) == 1
    assert selected_events[0].tournament.id == tournament.id
    assert len(selected_events[0].participants) == 2
    await prefetch_related_objects([participants[0]], "events")
    assert participants[0].events[0] == event

    await prefetch_related_objects(participants, "events")

    await Team.objects.filter(events__tournament__id=tournament.id)

    await Event.objects.filter(tournament=tournament)

    await Tournament.objects.filter(events__name__in=["Test", "Prod"]).distinct()

    result = await Event.objects.filter(pk=event.pk).values("event_id", "name", tournament="tournament__name")
    assert result[0]["tournament"] == tournament.name

    result = await Event.objects.filter(pk=event.pk).values_list("event_id", "participants__name")
    assert len(result) == 2


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_m2m_add_concurrent_same_pair_does_not_duplicate_or_raise(db):
    """add() dedupes via INSERT ... ON CONFLICT DO NOTHING for the default unique=True M2M
    config - two concurrent add() calls for the same pair must not raise IntegrityError
    (the old SELECT-then-INSERT had a TOCTOU race here) or leave a duplicate row."""
    tournament = await Tournament.objects.create(name="Concurrent Tournament")
    event = await Event.objects.create(name="Concurrent Event", tournament_id=tournament.id)
    team = await Team.objects.create(name="Concurrent Team")

    await asyncio.gather(
        event.participants.add(team),
        event.participants.add(team),
    )

    teamids = [t.id async for t in event.participants]
    assert teamids == [team.id]


@pytest.mark.asyncio
async def test_reset_queryset_on_query(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    participants = []
    for i in range(2):
        team = await Team.objects.create(name=f"Team {(i + 1)}")
        participants.append(team)
    await event.participants.add(*participants)
    queryset = Event.objects.all().annotate(count=Count("participants"))
    assert (await queryset.first()).count == 2
    assert (await queryset.filter(name="Test").first()).count == 2


@pytest.mark.asyncio
async def test_bool_for_relation_new_object(db):
    tournament = await Tournament.objects.create(name="New Tournament")

    with pytest.raises(NoValuesFetched):
        bool(tournament.events)


@pytest.mark.asyncio
async def test_bool_for_relation_old_object(db):
    await Tournament.objects.create(name="New Tournament")
    tournament = await Tournament.objects.first()

    with pytest.raises(NoValuesFetched):
        bool(tournament.events)


@pytest.mark.asyncio
async def test_bool_for_relation_fetched_false(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await prefetch_related_objects([tournament], "events")

    assert not bool(tournament.events)


@pytest.mark.asyncio
async def test_bool_for_relation_fetched_true(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)
    await prefetch_related_objects([tournament], "events")

    assert bool(tournament.events)


@pytest.mark.asyncio
async def test_m2m_add(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 2


@pytest.mark.asyncio
async def test_m2m_add_already_added(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    await event.participants.add(team, team_second)
    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 2


@pytest.mark.asyncio
async def test_m2m_add_already_added_non_unique_field_with_instance_dependent_pk(db):
    """ManyToManyRelation.add()'s non-unique dedup path (only reachable for a M2M field declared
    with unique=False - the default unique=True path takes an INSERT ... ON CONFLICT DO NOTHING
    shortcut instead) passed the OWNING side's instance to the RELATED model's own pk field
    to_db_value(), instead of a related-model instance - for a pk field type whose to_db_value
    depends on `instance` (e.g. a DatetimeField pk with auto_now_add, which reads
    getattr(instance, model_field_name)), this crashed with AttributeError, since the owning
    instance is a different model class entirely and doesn't have that attribute."""
    from tests.testmodels import TimestampPkOwner, TimestampPkTarget

    owner = await TimestampPkOwner.objects.create(name="Owner1")
    target = await TimestampPkTarget.objects.create(name="Target1")

    await owner.targets.add(target)
    await owner.targets.add(target)

    assert len(await owner.targets.all()) == 1


@pytest.mark.asyncio
async def test_m2m_clear(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    await event.participants.clear()
    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 0


@pytest.mark.asyncio
async def test_m2m_remove(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    await event.participants.remove(team)
    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 1


@pytest.mark.asyncio
async def test_m2m_set_replaces_the_whole_relation(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team_a = await Team.objects.create(name="A")
    team_b = await Team.objects.create(name="B")
    team_c = await Team.objects.create(name="C")
    await event.participants.add(team_a, team_b)

    await event.participants.set(team_b, team_c)

    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert {t.name for t in fetched_event.participants} == {"B", "C"}


@pytest.mark.asyncio
async def test_m2m_set_no_instances_clears(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    await event.participants.add(team)

    await event.participants.set()

    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 0


@pytest.mark.asyncio
async def test_m2m_second_hop_in_a_lookup_chain_is_not_dropped(db):
    """LookupPaths.get_joins_for_related_field's ManyToMany branch never aliased the M2M
    through table (unlike related_table, which always is) - a lookup chain with a SECOND M2M hop
    (e.g. participants__events__tournament) reused the identical, unaliased Table object for
    both hops' through joins. QueryBuilder.do_join()/is_joined() compare joined tables by
    identity/equality, so the second hop's own through join was silently dropped as "already
    joined", pinning the rest of the chain to the FIRST hop's through row instead of genuinely
    walking to the second hop."""
    tournament1 = await Tournament.objects.create(name="T1")
    tournament2 = await Tournament.objects.create(name="T2")
    event1 = await Event.objects.create(name="E1", tournament=tournament1)
    event2 = await Event.objects.create(name="E2", tournament=tournament2)
    team = await Team.objects.create(name="A")
    await event1.participants.add(team)
    await event2.participants.add(team)

    result = (
        await Event.objects.filter(participants__events__tournament__name="T2")
        .order_by("name")
        .values_list("name", flat=True)
    )
    # team A plays both E1 (tournament T1) and E2 (tournament T2) - filtering "events whose
    # participants also play in a T2 event" must find BOTH E1 and E2 through team A, not just
    # the event that directly IS in T2.
    assert result == ["E1", "E2"]

    result_3_hop = await Event.objects.filter(participants__events__participants__name="A").values_list(
        "name", flat=True
    )
    assert set(result_3_hop) == {"E1", "E2"}


@pytest.mark.asyncio
async def test_o2o_lazy(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event)

    fetched_address = await event.address
    assert fetched_address.city == "Santa Monica"


@pytest.mark.asyncio
async def test_m2m_remove_two(db):
    tournament = await Tournament.objects.create(name="tournament")
    event = await Event.objects.create(name="First", tournament=tournament)
    team = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event.participants.add(team, team_second)
    await event.participants.remove(team, team_second)
    fetched_event = await Event.objects.first().prefetch_related("participants")
    assert len(fetched_event.participants) == 0


@pytest.mark.asyncio
async def test_self_ref(db):
    root = await Employee.objects.create(name="Root")
    loose = await Employee.objects.create(name="Loose")
    _1 = await Employee.objects.create(name="1. First H1", manager=root)
    _2 = await Employee.objects.create(name="2. Second H1", manager=root)
    _1_1 = await Employee.objects.create(name="1.1. First H2", manager=_1)
    _1_1_1 = await Employee.objects.create(name="1.1.1. First H3", manager=_1_1)
    _2_1 = await Employee.objects.create(name="2.1. Second H2", manager=_2)
    _2_2 = await Employee.objects.create(name="2.2. Third H2", manager=_2)

    await _1.talks_to.add(_2, _1_1_1, loose)
    await _2_1.gets_talked_to.add(_2_2, _1_1, loose)

    LOOSE_TEXT = "Loose (to: 2.1. Second H2) (from: 1. First H1)"
    ROOT_TEXT = """Root (to: ) (from: )
  1. First H1 (to: 1.1.1. First H3, 2. Second H1, Loose) (from: )
    1.1. First H2 (to: 2.1. Second H2) (from: )
      1.1.1. First H3 (to: ) (from: 1. First H1)
  2. Second H1 (to: ) (from: 1. First H1)
    2.1. Second H2 (to: ) (from: 1.1. First H2, 2.2. Third H2, Loose)
    2.2. Third H2 (to: 2.1. Second H2) (from: )"""

    # Evaluated off creation objects
    assert await loose.full_hierarchy__async_for() == LOOSE_TEXT
    assert await loose.full_hierarchy__fetch_related() == LOOSE_TEXT
    assert await root.full_hierarchy__async_for() == ROOT_TEXT
    assert await root.full_hierarchy__fetch_related() == ROOT_TEXT

    # Evaluated off new objects -> Result is identical
    root2 = await Employee.objects.get(name="Root")
    loose2 = await Employee.objects.get(name="Loose")
    assert await loose2.full_hierarchy__async_for() == LOOSE_TEXT
    assert await loose2.full_hierarchy__fetch_related() == LOOSE_TEXT
    assert await root2.full_hierarchy__async_for() == ROOT_TEXT
    assert await root2.full_hierarchy__fetch_related() == ROOT_TEXT


@pytest.mark.asyncio
async def test_self_ref_filter_by_child(db):
    root = await Employee.objects.create(name="Root")
    await Employee.objects.create(name="1. First H1", manager=root)
    await Employee.objects.create(name="2. Second H1", manager=root)

    root2 = await Employee.objects.get(team_members__name="1. First H1")
    assert root.id == root2.id


@pytest.mark.asyncio
async def test_self_ref_filter_both(db):
    root = await Employee.objects.create(name="Root")
    await Employee.objects.create(name="1. First H1", manager=root)
    await Employee.objects.create(name="2. Second H1", manager=root)

    root2 = await Employee.objects.get(name="Root", team_members__name="1. First H1")
    assert root.id == root2.id


@pytest.mark.asyncio
async def test_self_ref_annotate(db):
    root = await Employee.objects.create(name="Root")
    await Employee.objects.create(name="Loose")
    await Employee.objects.create(name="1. First H1", manager=root)
    await Employee.objects.create(name="2. Second H1", manager=root)

    root_ann = await Employee.objects.get(name="Root").annotate(num_team_members=Count("team_members"))
    assert root_ann.num_team_members == 2
    root_ann = await Employee.objects.get(name="Loose").annotate(num_team_members=Count("team_members"))
    assert root_ann.num_team_members == 0


@pytest.mark.asyncio
async def test_prefetch_related_fk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    event2 = await Event.objects.filter(name="Test").prefetch_related("tournament")
    assert event2[0].tournament == tournament


@pytest.mark.asyncio
async def test_prefetch_related_rfk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)

    tournament2 = await Tournament.objects.filter(name="New Tournament").prefetch_related("events")
    assert list(tournament2[0].events) == [event]


@pytest.mark.parametrize(
    ("error_match", "field_name"),
    [
        pytest.param(
            "Relation tourn1ment for models.Event not found", "tourn1ment", id="prefetch_related_missing_field"
        ),
        pytest.param(
            "Field modified on models.Event is not a relation", "modified", id="prefetch_related_nonrel_field"
        ),
        pytest.param("Field event_id on models.Event is not a relation", "event_id", id="prefetch_related_id"),
    ],
)
@pytest.mark.asyncio
async def test_prefetch_related_of_a_field_that_is_no_relation(db, error_match, field_name):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match=error_match):
        await Event.objects.filter(name="Test").prefetch_related(field_name)


@pytest.mark.asyncio
async def test_nullable_fk_raw(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    reporter = await Reporter.objects.create(name="Reporter")
    event1 = await Event.objects.create(name="Without reporter", tournament=tournament)
    event2 = await Event.objects.create(name="With reporter", tournament=tournament, reporter=reporter)

    assert not event1.reporter_id
    assert event2.reporter_id


@pytest.mark.asyncio
async def test_nullable_fk_obj(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    reporter = await Reporter.objects.create(name="Reporter")
    event1 = await Event.objects.create(name="Without reporter", tournament=tournament)
    event2 = await Event.objects.create(name="With reporter", tournament=tournament, reporter=reporter)

    assert not event1.reporter
    assert event2.reporter


@pytest.mark.asyncio
async def test_db_constraint(db):
    author = await Author.objects.create(name="Some One")
    book = await BookNoConstraint.objects.create(name="First!", author=author, rating=4)
    book = await BookNoConstraint.objects.all().select_related("author").get(pk=book.pk)
    assert author.pk == book.author.pk


@pytest.mark.asyncio
async def test_select_related_with_annotation(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    reporter = await Reporter.objects.create(name="Reporter")
    event = await Event.objects.create(name="With reporter", tournament=tournament, reporter=reporter)
    event = (
        await Event.objects.filter(pk=event.pk)
        .select_related("reporter")
        .annotate(tournament_name=Trim("tournament__name"))
        .first()
    )
    assert event.reporter == reporter
    assert hasattr(event, "tournament_name")
    assert event.tournament_name == tournament.name


@pytest.mark.asyncio
async def test_select_related_sets_null_for_null_fk(db):
    """Test that select related yields null for fields with nulled fk cols."""
    related_dude = await UUIDFkRelatedNullModel.objects.create(name="Some model")
    await prefetch_related_objects([related_dude], "parent")  # that is strange :)
    related_dude_fresh = await UUIDFkRelatedNullModel.objects.all().select_related("parent").get(id=related_dude.id)
    assert related_dude_fresh.parent is None
    assert related_dude_fresh.parent == related_dude.parent


@pytest.mark.asyncio
async def test_select_related_sets_valid_nulls(db) -> None:
    """When we select related objects, the data we get from db should be set to corresponding attribute."""
    left_2nd_lvl = await DoubleFK.objects.create(name="second leaf")
    left_1st_lvl = await DoubleFK.objects.create(name="1st", left=left_2nd_lvl)
    root = await DoubleFK.objects.create(name="root", left=left_1st_lvl)

    retrieved_root = await DoubleFK.objects.all().select_related("left__left__left", "right").get(id=root.pk)
    assert retrieved_root.right is None
    assert retrieved_root.left is not None
    assert retrieved_root.left == left_1st_lvl
    assert retrieved_root.left.left == left_2nd_lvl


@pytest.mark.asyncio
async def test_no_ambiguous_fk_relations_set(db):
    """Basic select_related test cases provided by @https://github.com/Terrance.

    The idea was that on the moment of writing this feature, there were no way to correctly set attributes for
    select_related fields attributes.
    src: https://github.com/hare/hare-orm/pull/826#issuecomment-883341557
    """

    extra = await Extra.objects.create()
    single = await Single.objects.create(extra=extra)
    await Pair.objects.create(right=single)
    pair = await Pair.objects.filter(id=1).select_related("left", "left__extra", "right", "right__extra").get()
    assert pair.left is None
    assert pair.right.extra == extra
    single = await Single.objects.create()
    await Pair.objects.create(right=single)
    pair = await Pair.objects.filter(id=2).select_related("left", "left__extra", "right", "right__extra").get()
    assert pair.right.extra is None  # should be None


@pytest.mark.asyncio
async def test_0_value_fk(db):
    """ForegnKeyField should exits even if the the source_field looks like false, but not None
    src: https://github.com/hare/hare-orm/issues/1274
    """
    extra = await Extra.objects.create(id=0)
    single = await Single.objects.create(extra=extra)

    single_reload = await Single.objects.get(id=single.id)
    assert (await single_reload.extra).id == 0

    tournament_0 = await Tournament.objects.create(name="tournament zero", id=0)
    await Event.objects.create(name="event-zero", tournament=tournament_0)

    e = await Event.objects.get(name="event-zero")
    id_before_fetch = e.tournament_id
    await prefetch_related_objects([e], "tournament")
    id_after_fetch = e.tournament_id
    assert id_before_fetch == id_after_fetch

    event_0 = await Event.objects.get(name="event-zero").prefetch_related("tournament")
    assert event_0.tournament == tournament_0


# =============================================================================
# TestDoubleFK - uses db fixture with setup data
# =============================================================================


# Regex patterns for SQL query validation
_select_match = r'SELECT [`"]doublefk[`"].[`"]name[`"] [`"]name[`"]'
_select1_match = r'[`"]doublefk__left[`"].[`"]name[`"] [`"]left__name[`"]'
_select2_match = r'[`"]doublefk__right[`"].[`"]name[`"] [`"]right__name[`"]'
_join1_match = (
    r'LEFT OUTER JOIN [`"]doublefk[`"] [`"]doublefk__left[`"] ON '
    r'[`"]doublefk__left[`"].[`"]id[`"]=[`"]doublefk[`"].[`"]left_id[`"]'
)
_join2_match = (
    r'LEFT OUTER JOIN [`"]doublefk[`"] [`"]doublefk__right[`"] ON '
    r'[`"]doublefk__right[`"].[`"]id[`"]=[`"]doublefk[`"].[`"]right_id[`"]'
)


@pytest_asyncio.fixture
async def doublefk_data(db):
    """Build DoubleFK test data."""
    one = await DoubleFK.objects.create(name="one")
    two = await DoubleFK.objects.create(name="two")
    middle = await DoubleFK.objects.create(name="middle", left=one, right=two)
    return middle


@pytest.mark.asyncio
async def test_doublefk_filter(db, doublefk_data):
    middle = doublefk_data
    qset = DoubleFK.objects.filter(left__name="one")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_join1_match, query)
    assert result == [middle]


@pytest.mark.asyncio
async def test_doublefk_filter_values(db, doublefk_data):
    qset = DoubleFK.objects.filter(left__name="one").values("name")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_select_match, query)
    assert re.search(_join1_match, query)
    assert result == [{"name": "middle"}]


@pytest.mark.asyncio
async def test_doublefk_filter_values_rel(db, doublefk_data):
    qset = DoubleFK.objects.filter(left__name="one").values("name", "left__name")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_select_match, query)
    assert re.search(_select1_match, query)
    assert re.search(_join1_match, query)
    assert result == [{"name": "middle", "left__name": "one"}]


@pytest.mark.asyncio
async def test_doublefk_filter_both(db, doublefk_data):
    middle = doublefk_data
    qset = DoubleFK.objects.filter(left__name="one", right__name="two")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_join1_match, query)
    assert re.search(_join2_match, query)
    assert result == [middle]


@pytest.mark.asyncio
async def test_doublefk_filter_both_values(db, doublefk_data):
    qset = DoubleFK.objects.filter(left__name="one", right__name="two").values("name")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_select_match, query)
    assert re.search(_join1_match, query)
    assert re.search(_join2_match, query)
    assert result == [{"name": "middle"}]


@pytest.mark.asyncio
async def test_doublefk_filter_both_values_rel(db, doublefk_data):
    qset = DoubleFK.objects.filter(left__name="one", right__name="two").values("name", "left__name", "right__name")
    result = await qset
    query = qset.sql(params_inline=True)

    assert re.search(_select_match, query)
    assert re.search(_select1_match, query)
    assert re.search(_select2_match, query)
    assert re.search(_join1_match, query)
    assert re.search(_join2_match, query)
    assert result == [{"name": "middle", "left__name": "one", "right__name": "two"}]


@pytest.mark.asyncio
async def test_many2many_field_with_o2o_fk(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    address = await Address.objects.create(city="c", street="s", event=event)
    obj = await M2mWithO2oPk.objects.create(name="m")
    assert await obj.address.all() == []
    await obj.address.add(address)
    assert await obj.address.all() == [address]


@pytest.mark.asyncio
async def test_o2o_fk_model_with_m2m_field(db):
    author = await Author.objects.create(name="a")
    obj = await O2oPkModelWithM2m.objects.create(author=author)
    node = await Node.objects.create(name="n")
    assert await obj.nodes.all() == []
    await obj.nodes.add(node)
    assert await obj.nodes.all() == [node]


@pytest.mark.asyncio
async def test_pk_filter_on_model_with_o2o_as_pk(db):
    """A model whose primary key IS a single OneToOneField(primary_key=True) (Address, keyed
    by its "event" O2O field) must still support the "pk"/"pk__in" filter alias, same as any
    plain-scalar-pk model - it previously had no "pk" filter registered at all, since
    ModelMeta._dispatch_fields() only ever registered it for non-relation fields."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    address = await Address.objects.create(city="c", street="s", event=event)

    assert await Address.objects.filter(pk=address.pk).first() == address
    assert await Address.objects.get(pk=address.pk) == address
    assert await Address.objects.filter(pk__in=[address.pk]) == [address]

    await address.refresh_from_db()
    assert address.city == "c"


@pytest.mark.asyncio
async def test_m2m_prefetch_target_with_o2o_as_pk(db):
    """.prefetch_related() on an M2M field whose TARGET model's own primary key is a single
    OneToOneField(primary_key=True) (Address) - the M2M prefetch strategy filters the target
    side by its own pk, which crashed with "Unknown filter param 'pk__in'" before the "pk"
    filter alias existed for an O2O-as-pk model."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    address = await Address.objects.create(city="c", street="s", event=event)
    owner = await M2mWithO2oPk.objects.create(name="m")
    await owner.address.add(address)

    fetched = await M2mWithO2oPk.objects.filter(pk=owner.pk).prefetch_related("address").first()

    assert await fetched.address.all() == [address]


@pytest.mark.asyncio
async def test_reverse_relation_create_fk(db):
    tournament = await Tournament.objects.create(name="Test Tournament")
    assert await tournament.events.all() == []

    event = await tournament.events.create(name="Test Event")

    await prefetch_related_objects([tournament], "events")

    assert len(tournament.events) == 1
    assert event.name == "Test Event"
    assert event.tournament_id == tournament.id
    assert tournament.events[0].event_id == event.event_id


@pytest.mark.asyncio
async def test_reverse_relation_create_fk_errors_for_unsaved_instance(db):
    tournament = Tournament(name="Unsaved Tournament")

    # Should raise OperationalError since tournament isn't saved
    with pytest.raises(QueryError) as cm:
        await tournament.events.create(name="Test Event")

    assert "hasn't been instanced" in str(cm.value)


# =============================================================================
# Nullable to_field targets, FK to a OneToOneField primary key, reverse/M2M create()
# =============================================================================


@pytest.mark.asyncio
async def test_reverse_relation_of_null_to_field_value_matches_no_row(db):
    russia = await NullableCodeCountry.objects.create(code="RU")
    await NullableCodeCity.objects.create(name="msk", country=russia)
    await NullableCodeCity.objects.create(name="orphan")
    await NullableCodeCapital.objects.create(name="orphan capital")
    no_code = await NullableCodeCountry.objects.create(code=None)

    assert await no_code.cities.all() == []
    assert await no_code.cities.filter(name="orphan") == []
    assert await no_code.capital is None
    await prefetch_related_objects([no_code], "cities")
    assert list(no_code.cities) == []

    countries = (
        await NullableCodeCountry.objects.filter(id__in=[russia.id, no_code.id])
        .order_by("id")
        .prefetch_related("cities", "capital")
    )
    assert [[city.name for city in country.cities] for country in countries] == [["msk"], []]
    assert [country.capital for country in countries] == [None, None]


@pytest.mark.asyncio
async def test_reverse_relation_create_on_null_to_field_value_raises_before_insert(db):
    """create() through a parent whose to_field value is NULL would insert a row with a NULL FK,
    detached from that parent."""
    no_code = await NullableCodeCountry.objects.create(code=None)

    with pytest.raises(QueryError, match="NullableCodeCountry's code is NULL"):
        await no_code.cities.create(name="orphan")
    assert await NullableCodeCity.objects.filter(name="orphan").count() == 0


@pytest.mark.asyncio
async def test_fk_to_model_with_o2o_primary_key_stores_the_target_pk(db):
    account = await O2oPkAccount.objects.create(name="a")
    profile = await O2oPkProfile.objects.create(account=account, bio="b")

    note_field = O2oPkProfileNote._meta.fields_map["profile"]
    assert note_field.to_field == "account_id"
    assert [field.model_field_name for field in note_field.to_field_instances] == ["account_id"]
    assert "profile_id" in O2oPkProfileNote._meta.db_fields

    note = await O2oPkProfileNote.objects.create(text="n", profile=profile)
    assert note.profile_id == account.pk
    fetched_note = await O2oPkProfileNote.objects.get(pk=note.pk).select_related("profile")
    assert fetched_note.profile_id == account.pk
    assert fetched_note.profile.pk == account.pk
    assert [row.pk for row in await profile.notes.all()] == [note.pk]
    assert [row.pk for row in await O2oPkProfileNote.objects.filter(profile=profile)] == [note.pk]
    prefetched_profile = await O2oPkProfile.objects.get(pk=account.pk).prefetch_related("notes")
    assert [row.pk for row in prefetched_profile.notes] == [note.pk]

    await profile.delete()
    assert await O2oPkProfileNote.objects.filter(pk=note.pk).count() == 0


@pytest.mark.asyncio
async def test_reverse_create_invalidates_fetched_relation(db):
    tournament = await Tournament.objects.create(name="cup")
    await Event.objects.create(name="first", tournament=tournament)
    await prefetch_related_objects([tournament], "events")
    assert [event.name for event in tournament.events] == ["first"]

    await tournament.events.create(name="second")

    with pytest.raises(NoValuesFetched):
        list(tournament.events)
    assert sorted([event.name async for event in tournament.events]) == ["first", "second"]


@pytest.mark.asyncio
async def test_reverse_create_accepts_matching_relation_kwarg_through_to_field(db):
    parent = await ProtectedParentWithCode.objects.create(code=42)
    other_parent = await ProtectedParentWithCode.objects.create(code=43)

    child = await parent.children.create(name="c", parent=parent)
    assert child.parent_id == 42
    with pytest.raises(QueryError):
        await parent.children.create(name="c", parent=other_parent)


@pytest.mark.asyncio
async def test_reverse_create_accepts_matching_relation_kwarg_for_composite_target(db):
    document = await VersionedDocument.objects.create(title="v1")
    other_document = await VersionedDocument.objects.create(title="other")

    note = await document.revision_notes.create(note="n", document=document)
    assert (note.document_id, note.document_version) == (document.id, document.version)
    with pytest.raises(QueryError):
        await document.revision_notes.create(note="n", document=other_document)
    assert await DocumentRevisionNote.objects.filter(document=other_document).count() == 0


@pytest.mark.asyncio
async def test_m2m_create_creates_and_adds_the_related_object(db):
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    await prefetch_related_objects([event], "participants")

    team = await event.participants.create(name="created")

    assert team._saved_in_db
    with pytest.raises(NoValuesFetched):
        list(event.participants)
    assert [participant.pk for participant in await event.participants.all()] == [team.pk]
    assert [related_event.pk for related_event in await team.events.all()] == [event.pk]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_m2m_create_rolls_back_the_created_object_when_add_fails(db):
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    team_count = await Team.objects.all().count()

    with pytest.raises(QueryError):
        await event.participants.create(name="created", through_defaults={"missing": 1})

    assert await Team.objects.all().count() == team_count
