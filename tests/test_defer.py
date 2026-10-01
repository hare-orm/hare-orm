import pytest
import pytest_asyncio

from hare.contrib import test as hare_test
from hare.contrib.test import requires_features
from hare.exceptions import FieldError, IncompleteInstanceError
from hare.query.expressions import Q
from hare.query.functions import Count
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from tests.testmodels import (
    Address,
    CompositePkThing,
    Event,
    LazyJoinedChild,
    LazyJoinedParent,
    LazySelectChild,
    LazySelectParent,
    Reporter,
    SoftDeleteStandalone,
    StraightFields,
    Team,
    Tournament,
    VersionedDocument,
    VersionedThing,
)

# ============================================================================
# Basic get/filter/first
# ============================================================================


@pytest_asyncio.fixture
async def straight_fields_instance(db):
    return await StraightFields.objects.create(chars="Test")


@pytest.mark.asyncio
async def test_defer_get(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").defer("nullable", "blip")

    assert instance_part.chars == "Test"
    assert instance_part.eyedee is not None
    with pytest.raises(AttributeError):
        _ = instance_part.nullable
    with pytest.raises(AttributeError):
        _ = instance_part.blip


@pytest.mark.asyncio
async def test_defer_filter(db, straight_fields_instance):
    instances = await StraightFields.objects.filter(chars="Test").defer("nullable", "blip")

    assert len(instances) == 1
    assert instances[0].chars == "Test"
    with pytest.raises(AttributeError):
        _ = instances[0].nullable


@pytest.mark.asyncio
async def test_defer_first(db, straight_fields_instance):
    instance_part = await StraightFields.objects.filter(chars="Test").defer("nullable", "blip").first()

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.nullable


# ============================================================================
# save()/update_fields interaction (mirrors test_only.py's TestOnlyStraight)
# ============================================================================


@pytest.mark.asyncio
async def test_defer_save_incomplete(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").defer("nullable", "blip")

    with pytest.raises(IncompleteInstanceError, match=" is a partial model"):
        await instance_part.save()


@pytest.mark.asyncio
async def test_defer_partial_save_no_pk(db, straight_fields_instance):
    """Deferring the PK itself means partial update can never work, even for a field that
    wasn't deferred."""
    instance_part = await StraightFields.objects.get(chars="Test").defer("eyedee")

    with pytest.raises(IncompleteInstanceError, match="Partial update not available"):
        await instance_part.save(update_fields=["chars"])


@pytest.mark.asyncio
async def test_defer_partial_save_deferred_field(db, straight_fields_instance):
    """PK is present (not deferred), but the field being updated was deferred."""
    instance_part = await StraightFields.objects.get(chars="Test").defer("nullable")

    with pytest.raises(IncompleteInstanceError, match="field 'nullable' is not available"):
        await instance_part.save(update_fields=["nullable"])


@pytest.mark.asyncio
async def test_defer_partial_save_with_pk(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").defer("nullable", "blip")

    instance_part.chars = "Test1"
    await instance_part.save(update_fields=["chars"])

    instance2 = await StraightFields.objects.get(pk=straight_fields_instance.pk)
    assert instance2.chars == "Test1"


@pytest.mark.asyncio
async def test_defer_excludes_pk_when_deferred(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").defer("eyedee")

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.eyedee


# ============================================================================
# Validation
# ============================================================================


@pytest.mark.asyncio
async def test_defer_empty_raises(db, straight_fields_instance):
    with pytest.raises(ValueError):
        await StraightFields.objects.all().defer()


@pytest.mark.asyncio
async def test_defer_nonexistent_field_raises(db, straight_fields_instance):
    with pytest.raises(FieldError):
        await StraightFields.objects.all().defer("nonexistent_field")


@pytest.mark.asyncio
async def test_defer_relation_name_raises(db):
    """A relation attribute name (not its shadow *_id column) isn't a direct field."""
    with pytest.raises(FieldError):
        await Event.objects.all().defer("tournament")


# ============================================================================
# Combined with other QuerySet operations (mirrors test_only.py's TestOnlyAdvanced)
# ============================================================================


@pytest_asyncio.fixture
async def tournament_with_events(db):
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    event1 = await Event.objects.create(name="Event 1", tournament=tournament)
    event2 = await Event.objects.create(name="Event 2", tournament=tournament)
    return tournament, event1, event2


@pytest.mark.asyncio
async def test_defer_advanced_exclude(db, tournament_with_events):
    tournament, event1, event2 = tournament_with_events
    events = (
        await Event.objects.filter(tournament=tournament).exclude(name="Event 2").defer("modified", "token", "alias")
    )
    assert len(events) == 1
    assert events[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = events[0].modified


@pytest.mark.asyncio
async def test_defer_advanced_limit(db, tournament_with_events):
    events = await Event.objects.all().defer("modified", "token", "alias").order_by("name").limit(1)
    assert len(events) == 1
    assert events[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = events[0].modified


@pytest.mark.asyncio
async def test_defer_advanced_distinct(db, tournament_with_events):
    tournament, event1, event2 = tournament_with_events
    await Event.objects.create(name="Event 1", tournament=tournament)

    events = await Event.objects.all().defer("modified", "token", "alias", "event_id").distinct()
    assert len(events) == 2
    assert {e.name for e in events} == {"Event 1", "Event 2"}


@pytest.mark.asyncio
async def test_defer_advanced_values_raises(db, tournament_with_events):
    with pytest.raises(ValueError):
        await Event.objects.all().defer("modified").values("name")


@pytest.mark.asyncio
async def test_defer_advanced_values_list_raises(db, tournament_with_events):
    with pytest.raises(ValueError):
        await Event.objects.all().defer("modified").values_list("name")


@pytest.mark.asyncio
async def test_defer_and_only_mutually_exclusive(db, tournament_with_events):
    with pytest.raises(ValueError):
        Event.objects.all().only("name").defer("modified")
    with pytest.raises(ValueError):
        Event.objects.all().defer("modified").only("name")


@pytest.mark.asyncio
async def test_defer_advanced_annotate(db, tournament_with_events):
    tournaments = await Tournament.objects.annotate(event_count=Count("events")).defer("desc")

    assert tournaments[0].name == "Tournament A"
    assert tournaments[0].event_count == 2
    with pytest.raises(AttributeError):
        _ = tournaments[0].desc


@pytest.mark.asyncio
async def test_defer_advanced_join_in_filter(db, tournament_with_events):
    event = await Event.objects.filter(tournament__name="Tournament A").defer("modified", "token", "alias").first()
    assert event.name == "Event 1"


@pytest.mark.asyncio
async def test_defer_advanced_join_in_order_by(db, tournament_with_events):
    events = await Event.objects.all().order_by("tournament__name", "name").defer("modified", "token", "alias")
    assert events[0].name == "Event 1"


@pytest.mark.asyncio
async def test_defer_advanced_select_related(db, tournament_with_events):
    """defer() only ever governs the base table's own columns - the related table's columns
    hydrated by select_related() are untouched."""
    event = (
        await Event.objects.filter(name="Event 1")
        .select_related("tournament")
        .defer("modified", "token", "alias")
        .first()
    )

    assert event.name == "Event 1"
    assert event.tournament.name == "Tournament A"
    with pytest.raises(AttributeError):
        _ = event.modified


@pytest_asyncio.fixture
async def event_with_reporter(db):
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    reporter = await Reporter.objects.create(name="Reporter A")
    event = await Event.objects.create(name="Event 1", tournament=tournament, reporter=reporter)
    return event, reporter


@pytest.mark.asyncio
async def test_only_with_select_related_relation_not_named_in_only(db, event_with_reporter):
    """.only() otherwise merges in whatever local (base-model) shadow FK column
    .prefetch_related() needs even when the caller didn't name it - .select_related() has the
    exact same requirement for its own forward-relation JOIN (the base row's own `reporter_id`
    column) but never got the same treatment, so a relation named only via .select_related(...)
    (not also spelled out inside .only(...)) used to crash accessing the relation afterward."""
    __, reporter = event_with_reporter

    fetched = await Event.objects.filter(name="Event 1").only("name").select_related("reporter").first()
    assert fetched.name == "Event 1"
    assert fetched.reporter.name == reporter.name


@pytest.mark.asyncio
async def test_select_related_with_only_relation_not_named_in_only_reverse_call_order(db, event_with_reporter):
    """Same as above with .select_related() called before .only() - call order must not matter."""
    __, reporter = event_with_reporter

    fetched = await Event.objects.filter(name="Event 1").select_related("reporter").only("name").first()
    assert fetched.name == "Event 1"
    assert fetched.reporter.name == reporter.name


# ============================================================================
# Combined with prefetch_related, including M2M
# ============================================================================


@pytest_asyncio.fixture
async def event_with_participants(db):
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    event = await Event.objects.create(name="Event 1", tournament=tournament)
    team_a = await Team.objects.create(name="Team A")
    team_b = await Team.objects.create(name="Team B")
    await event.participants.add(team_a, team_b)
    return event, team_a, team_b


@pytest.mark.asyncio
async def test_defer_with_prefetch_m2m(db, event_with_participants):
    """Deferring a plain field on the base model must not interfere with an M2M
    prefetch_related() on the same queryset."""
    event, team_a, team_b = event_with_participants

    fetched = await Event.objects.all().defer("modified", "token", "alias").prefetch_related("participants").first()

    assert fetched.name == "Event 1"
    with pytest.raises(AttributeError):
        _ = fetched.modified
    assert {team.name for team in fetched.participants} == {"Team A", "Team B"}


@pytest.mark.asyncio
async def test_defer_with_prefetch_m2m_filtered(db, event_with_participants):
    """defer() on the base model combined with a Prefetch() object carrying its own filtered
    queryset for the M2M relation."""
    event, team_a, team_b = event_with_participants

    fetched = (
        await Event.objects.all()
        .defer("modified", "token", "alias")
        .prefetch_related(Prefetch("participants", queryset=Team.objects.filter(name="Team A")))
        .first()
    )

    assert fetched.name == "Event 1"
    assert [team.name for team in fetched.participants] == ["Team A"]


@pytest.mark.asyncio
async def test_defer_inside_prefetch_queryset(db, event_with_participants):
    """defer() applied to the RELATED model's own queryset inside a Prefetch() - not the base
    model's queryset."""
    event, team_a, team_b = event_with_participants

    fetched = (
        await Event.objects.all()
        .prefetch_related(Prefetch("participants", queryset=Team.objects.all().defer("name")))
        .first()
    )

    assert len(fetched.participants) == 2
    for team in fetched.participants:
        assert team.id is not None
        with pytest.raises(AttributeError):
            _ = team.name


@pytest.mark.asyncio
async def test_defer_on_the_matching_column_still_works_across_every_prefetch_strategy(db):
    """_ensure_only_includes_fields() forces the instance<->related-object matching column back
    into a Prefetch(queryset=...)'s own SELECT when a caller's .only() would otherwise exclude it
    - but it only ever looked at _fields_for_select, which .defer() doesn't populate until
    _make_query() runs. Deferring the matching column itself used to crash 3 of the 4 prefetch
    strategies with a raw AttributeError, and silently return an empty list for the 4th (M2M)."""
    tournament = await Tournament.objects.create(name="T")
    event = await Event.objects.create(name="E", tournament=tournament)
    await Address.objects.create(city="City", street="St", event=event)
    team = await Team.objects.create(name="Team")
    await event.participants.add(team)

    reverse_fk = (
        await Tournament.objects.all()
        .prefetch_related(Prefetch("events", queryset=Event.objects.all().defer("tournament_id", "name")))
        .first()
    )
    assert reverse_fk is not None
    assert [e.pk for e in reverse_fk.events] == [event.pk]

    direct_fk = (
        await Event.objects.filter(pk=event.pk)
        .prefetch_related(Prefetch("tournament", queryset=Tournament.objects.all().defer("id")))
        .first()
    )
    assert direct_fk is not None
    assert direct_fk.tournament.pk == tournament.pk

    reverse_o2o = (
        await Event.objects.filter(pk=event.pk)
        .prefetch_related(Prefetch("address", queryset=Address.objects.all().defer("event_id")))
        .first()
    )
    assert reverse_o2o is not None
    assert reverse_o2o.address.city == "City"

    m2m = (
        await Event.objects.filter(pk=event.pk)
        .prefetch_related(Prefetch("participants", queryset=Team.objects.all().defer("id")))
        .first()
    )
    assert m2m is not None
    assert [t.pk for t in m2m.participants] == [team.pk]


@pytest.mark.asyncio
async def test_defer_with_prefetch_reverse_fk(db):
    """defer() on the base model combined with prefetch_related() on a reverse FK (one-to-many,
    not M2M) relation."""
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    fetched = await Tournament.objects.all().defer("desc").prefetch_related("events").first()

    assert fetched.name == "Tournament A"
    with pytest.raises(AttributeError):
        _ = fetched.desc
    assert {e.name for e in fetched.events} == {"Event 1", "Event 2"}


@pytest.mark.asyncio
async def test_defer_pk_with_prefetch_reverse_fk_still_works(db):
    """Unlike a forward relation's shadow FK column, the PK IS directly nameable in .defer() - a
    caller could plausibly (if pointlessly) try to defer it. prefetch_related() on a reverse
    relation needs it internally regardless, so it must still be force-included."""
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = await Tournament.objects.all().defer("id").prefetch_related("events").first()

    assert fetched.name == "Tournament A"
    assert {e.name for e in fetched.events} == {"Event 1"}


@pytest.mark.asyncio
async def test_defer_pk_with_prefetch_m2m_still_works(db, event_with_participants):
    """Same as above, for an M2M prefetch - deferring the base model's own PK must not break the
    M2M through-table join, which needs that PK value internally."""
    event, team_a, team_b = event_with_participants

    fetched = await Event.objects.all().defer("event_id").prefetch_related("participants").first()

    assert fetched.name == "Event 1"
    assert {team.name for team in fetched.participants} == {"Team A", "Team B"}


# ============================================================================
# Further combinations: composite PK, soft_delete_field, optimistic_lock_field, lazy=, after_cursor,
# distinct_on, VersionedModel, Select(...) nested path
# ============================================================================


@pytest.mark.asyncio
async def test_defer_composite_pk_non_pk_field(db):
    """Deferring an ordinary field leaves both pk columns intact automatically - defer() only
    ever prunes what's explicitly named."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    partial = await CompositePkThing.objects.get(thing_id=1, revision=1).defer("name")

    with pytest.raises(AttributeError):
        _ = partial.name
    partial.name = "B"
    await partial.save(update_fields=["name"])
    assert (await CompositePkThing.objects.get(thing_id=1, revision=1)).name == "B"


@pytest.mark.asyncio
async def test_defer_composite_pk_member_field_delete_raises(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    partial = await CompositePkThing.objects.get(thing_id=1, revision=1).defer("thing_id")

    with pytest.raises(IncompleteInstanceError):
        await partial.delete()


@pytest.mark.asyncio
async def test_defer_soft_delete_field_delete_still_works(db):
    """Unlike deferring the pk, deferring the soft_delete_field itself is fine - delete() only
    ever WRITES that field (via _set_soft_delete_field), it never needs to read an existing
    value first, unlike optimistic_lock_field below."""
    obj = await SoftDeleteStandalone.objects.create(name="A")
    partial = await SoftDeleteStandalone.objects.get(pk=obj.pk).defer("deleted_at")

    await partial.delete()

    refreshed = await SoftDeleteStandalone.objects.include_deleted().get(pk=obj.pk)
    assert refreshed.deleted_at is not None


@pytest.mark.asyncio
async def test_defer_optimistic_lock_field_save_raises(db):
    """optimistic_lock_field is read (for the WHERE staleness check) and bumped on every save(), even
    when update_fields names some other field entirely - deferring it must be caught the same way
    a genuinely missing update_fields entry is, not crash with a raw AttributeError."""
    thing = await VersionedThing.objects.create(name="A")
    partial = await VersionedThing.objects.get(pk=thing.pk).defer("version")

    with pytest.raises(IncompleteInstanceError):
        partial.name = "B"
        await partial.save(update_fields=["name"])


@pytest.mark.asyncio
async def test_defer_with_lazy_select_relation(db):
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child", parent=parent)

    child = await LazySelectChild.objects.filter(name="Child").defer("name").first()

    assert child.parent.name == "Parent"


@pytest.mark.asyncio
async def test_defer_with_lazy_joined_relation(db):
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)

    child = await LazyJoinedChild.objects.filter(name="Child").defer("name").first()

    assert child.parent.name == "Parent"


@pytest.mark.asyncio
async def test_defer_with_after_cursor(db, tournament_with_events):
    tournament, event1, event2 = tournament_with_events

    rows = await Event.objects.all().order_by("name").after_cursor("Event 1").defer("modified", "token", "alias")

    assert [row.name for row in rows] == ["Event 2"]


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_defer_with_distinct_on(db):
    from tests.testmodels import IntFields

    await IntFields.objects.create(intnum=10, intnum_null=0)
    await IntFields.objects.create(intnum=11, intnum_null=0)
    await IntFields.objects.create(intnum=20, intnum_null=1)

    rows = await IntFields.objects.all().distinct("intnum_null").order_by("intnum_null", "intnum").defer("intnum")

    assert [row.intnum_null for row in rows] == [0, 1]


@pytest.mark.asyncio
async def test_defer_with_versioned_model(db):
    doc = await VersionedDocument.objects.create(title="D1")
    partial = await VersionedDocument.objects.get(id=doc.id, version=doc.version).defer("title")

    with pytest.raises(AttributeError):
        _ = partial.title
    # id/version stay available - defer() only prunes the named field
    assert partial.id == doc.id
    assert partial.version == doc.version


@pytest.mark.asyncio
async def test_defer_with_select_extra_condition_on_nested_path(db, tournament_with_events):
    """DoubleFK (used for the equivalent .only() test) has no spare direct field to defer besides
    the ones the assertions need - Event/Tournament's own relation chain is only one level deep,
    so this uses Event's "tournament" relation for the join and defers one of Event's own unrelated
    fields, rather than exercising a second nesting level (already covered for .only())."""
    tournament, event1, event2 = tournament_with_events

    fetched = (
        await Event.objects.filter(pk=event1.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Tournament A")))
        .defer("modified", "token", "alias")
        .first()
    )
    assert fetched.tournament is not None
    assert fetched.tournament.name == "Tournament A"

    fetched_no_match = (
        await Event.objects.filter(pk=event1.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="No Match")))
        .defer("modified", "token", "alias")
        .first()
    )
    assert fetched_no_match.tournament is None


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_chained_defer_calls_accumulate(db, straight_fields_instance):
    sql = StraightFields.objects.all().defer("nullable").defer("blip").sql()

    assert '"nullable"' not in sql
    assert '"blip"' not in sql
    assert '"chars"' in sql


@pytest.mark.asyncio
async def test_running_a_deferred_queryset_leaves_it_chainable(db, straight_fields_instance):
    """Building the query used to store the expanded field list in the queryset's .only() state,
    so a later .defer()/.only() on the same queryset raised as if both had been used."""
    queryset = StraightFields.objects.all().defer("nullable")
    first_sql = queryset.sql()
    fetched = await queryset

    assert queryset.sql() == first_sql
    assert [item.pk for item in await queryset] == [item.pk for item in fetched]
    narrowed_sql = queryset.defer("blip").sql()
    assert '"blip"' not in narrowed_sql
    assert '"nullable"' not in narrowed_sql
    with pytest.raises(ValueError, match="cannot be combined"):
        queryset.only("chars")
