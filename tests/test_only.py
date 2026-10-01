import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.exceptions import FieldError, IncompleteInstanceError, IntegrityError, NoValuesFetched, StaleObjectError
from hare.query.expressions import Q
from hare.query.functions import Count
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    AutoNowThing,
    CompositePkThing,
    DoubleFK,
    Event,
    LazySelectChild,
    LazySelectParent,
    SoftDeleteStandalone,
    SourceFields,
    StraightFields,
    Team,
    Tournament,
    VersionedDocument,
    VersionedThing,
    VersionedUniqueAutoNow,
)

# ============================================================================
# Fixtures for TestOnlyStraight and TestOnlySource
# ============================================================================


@pytest_asyncio.fixture
async def straight_fields_instance(db):
    """Create a StraightFields instance for testing."""
    return await StraightFields.objects.create(chars="Test")


@pytest_asyncio.fixture
async def source_fields_instance(db):
    """Create a SourceFields instance for testing."""
    return await SourceFields.objects.create(chars="Test")


# ============================================================================
# TestOnlyStraight tests
# ============================================================================


@pytest.mark.asyncio
async def test_only_straight_get(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").only("chars", "blip")

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.nullable


@pytest.mark.asyncio
async def test_only_straight_filter(db, straight_fields_instance):
    instances = await StraightFields.objects.filter(chars="Test").only("chars", "blip")

    assert len(instances) == 1
    assert instances[0].chars == "Test"
    with pytest.raises(AttributeError):
        _ = instances[0].nullable


@pytest.mark.asyncio
async def test_only_straight_first(db, straight_fields_instance):
    instance_part = await StraightFields.objects.filter(chars="Test").only("chars", "blip").first()

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.nullable


@pytest.mark.asyncio
async def test_only_straight_save(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").only("chars", "blip")

    with pytest.raises(IncompleteInstanceError, match=" is a partial model"):
        await instance_part.save()


@pytest.mark.asyncio
async def test_only_straight_partial_save(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").only("chars", "blip")

    with pytest.raises(IncompleteInstanceError, match="Partial update not available"):
        await instance_part.save(update_fields=["chars"])


@pytest.mark.asyncio
async def test_only_straight_partial_save_with_pk_wrong_field(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").only("chars", "eyedee")

    with pytest.raises(IncompleteInstanceError, match="field 'nullable' is not available"):
        await instance_part.save(update_fields=["nullable"])


@pytest.mark.asyncio
async def test_only_straight_partial_save_with_pk(db, straight_fields_instance):
    instance_part = await StraightFields.objects.get(chars="Test").only("chars", "eyedee")

    instance_part.chars = "Test1"
    await instance_part.save(update_fields=["chars"])

    instance2 = await StraightFields.objects.get(pk=straight_fields_instance.pk)
    assert instance2.chars == "Test1"


# ============================================================================
# TestOnlySource tests (same as Straight but with SourceFields model)
# ============================================================================


@pytest.mark.asyncio
async def test_only_source_get(db, source_fields_instance):
    instance_part = await SourceFields.objects.get(chars="Test").only("chars", "blip")

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.nullable


@pytest.mark.asyncio
async def test_only_source_filter(db, source_fields_instance):
    instances = await SourceFields.objects.filter(chars="Test").only("chars", "blip")

    assert len(instances) == 1
    assert instances[0].chars == "Test"
    with pytest.raises(AttributeError):
        _ = instances[0].nullable


@pytest.mark.asyncio
async def test_only_source_first(db, source_fields_instance):
    instance_part = await SourceFields.objects.filter(chars="Test").only("chars", "blip").first()

    assert instance_part.chars == "Test"
    with pytest.raises(AttributeError):
        _ = instance_part.nullable


@pytest.mark.asyncio
async def test_only_source_save(db, source_fields_instance):
    instance_part = await SourceFields.objects.get(chars="Test").only("chars", "blip")

    with pytest.raises(IncompleteInstanceError, match=" is a partial model"):
        await instance_part.save()


@pytest.mark.asyncio
async def test_only_source_partial_save(db, source_fields_instance):
    instance_part = await SourceFields.objects.get(chars="Test").only("chars", "blip")

    with pytest.raises(IncompleteInstanceError, match="Partial update not available"):
        await instance_part.save(update_fields=["chars"])


@pytest.mark.asyncio
async def test_only_source_partial_save_with_pk_wrong_field(db, source_fields_instance):
    instance_part = await SourceFields.objects.get(chars="Test").only("chars", "eyedee")

    with pytest.raises(IncompleteInstanceError, match="field 'nullable' is not available"):
        await instance_part.save(update_fields=["nullable"])


@pytest.mark.asyncio
async def test_only_source_partial_save_with_pk(db, source_fields_instance):
    instance_part = await SourceFields.objects.get(chars="Test").only("chars", "eyedee")

    instance_part.chars = "Test1"
    await instance_part.save(update_fields=["chars"])

    instance2 = await SourceFields.objects.get(pk=source_fields_instance.pk)
    assert instance2.chars == "Test1"


# ============================================================================
# TestOnlyRecursive tests
# ============================================================================


@pytest.mark.asyncio
async def test_only_recursive_one_level(db):
    left_1st_lvl = await DoubleFK.objects.create(name="1st")
    root = await DoubleFK.objects.create(name="root", left=left_1st_lvl)

    ret = await DoubleFK.objects.filter(pk=root.pk).only("name", "left__name", "left__left__name").first()
    assert ret is not None
    with pytest.raises(AttributeError):
        _ = ret.id
    assert ret.name == "root"
    assert ret.left.name == "1st"
    assert ret.left.id == left_1st_lvl.id
    with pytest.raises(AttributeError):
        _ = ret.right


@pytest.mark.asyncio
async def test_only_recursive_two_levels(db):
    left_2nd_lvl = await DoubleFK.objects.create(name="second leaf")
    left_1st_lvl = await DoubleFK.objects.create(name="1st", left=left_2nd_lvl)
    root = await DoubleFK.objects.create(name="root", left=left_1st_lvl)

    ret = await DoubleFK.objects.filter(pk=root.pk).only("name", "left__name", "left__left__name").first()
    assert ret is not None
    with pytest.raises(AttributeError):
        _ = ret.id
    assert ret.name == "root"
    assert ret.left.name == "1st"
    assert ret.left.id == left_1st_lvl.id
    assert ret.left.left.name == "second leaf"


@pytest.mark.asyncio
async def test_only_recursive_two_levels_reverse_argument_order(db):
    left_2nd_lvl = await DoubleFK.objects.create(name="second leaf")
    left_1st_lvl = await DoubleFK.objects.create(name="1st", left=left_2nd_lvl)
    root = await DoubleFK.objects.create(name="root", left=left_1st_lvl)

    ret = await DoubleFK.objects.filter(pk=root.pk).only("left__left__name", "left__name", "name").first()
    assert ret is not None
    with pytest.raises(AttributeError):
        _ = ret.id
    assert ret.name == "root"
    assert ret.left.name == "1st"
    assert ret.left.id == left_1st_lvl.id
    assert ret.left.left.name == "second leaf"


# ============================================================================
# TestOnlyRelated tests
# ============================================================================


@pytest.mark.asyncio
async def test_only_related_one_level(db):
    tournament = await Tournament.objects.create(name="New Tournament", desc="New Description")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    ret = await Event.objects.filter(tournament=tournament).only("name", "tournament__name").order_by("name")
    assert len(ret) == 2
    assert ret[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = ret[0].alias
    assert ret[1].name == "Event 2"
    with pytest.raises(AttributeError):
        _ = ret[1].alias
    assert ret[0].tournament.name == "New Tournament"
    assert ret[0].tournament.id == tournament.id
    with pytest.raises(AttributeError):
        _ = ret[0].tournament.desc


@pytest.mark.asyncio
async def test_only_related_one_level_reversed_argument_order(db):
    tournament = await Tournament.objects.create(name="New Tournament", desc="New Description")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    ret = await Event.objects.filter(tournament=tournament).only("tournament__name", "name").order_by("name")
    assert len(ret) == 2
    assert ret[0].name == "Event 1"
    assert ret[0].tournament.name == "New Tournament"


@pytest.mark.asyncio
async def test_only_related_just_related(db):
    tournament = await Tournament.objects.create(name="New Tournament", desc="New Description")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    ret = await Event.objects.filter(tournament=tournament).only("tournament__name").order_by("name").all()
    assert len(ret) == 2
    assert ret[0].tournament.name == "New Tournament"
    assert ret[1].tournament.name == "New Tournament"


# ============================================================================
# Fixture for TestOnlyAdvanced tests
# ============================================================================


@pytest_asyncio.fixture
async def tournament_with_events(db):
    """Create a tournament with two events for advanced tests."""
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    event1 = await Event.objects.create(name="Event 1", tournament=tournament)
    event2 = await Event.objects.create(name="Event 2", tournament=tournament)
    return tournament, event1, event2


# ============================================================================
# TestOnlyAdvanced tests
# ============================================================================


@pytest.mark.asyncio
async def test_only_advanced_exclude(db, tournament_with_events):
    """Test .only() combined with .exclude()"""
    tournament, event1, event2 = tournament_with_events
    events = await Event.objects.filter(tournament=tournament).exclude(name="Event 2").only("name")
    assert len(events) == 1
    assert events[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = events[0].modified


@pytest.mark.asyncio
async def test_only_advanced_limit(db, tournament_with_events):
    """Test .only() combined with .limit()"""
    events = await Event.objects.all().only("name").limit(1)
    assert len(events) == 1
    assert events[0].name == "Event 1"  # Assumes ordering by PK
    with pytest.raises(AttributeError):
        _ = events[0].modified


@pytest.mark.asyncio
async def test_only_advanced_distinct(db, tournament_with_events):
    """Test .only() combined with .distinct()"""
    tournament, event1, event2 = tournament_with_events
    # Create duplicate event names
    await Event.objects.create(name="Event 1", tournament=tournament)

    events = await Event.objects.all().only("name").distinct()
    # Should only have 2 distinct event names
    assert len(events) == 2
    event_names = {e.name for e in events}
    assert event_names == {"Event 1", "Event 2"}


@pytest.mark.asyncio
async def test_only_advanced_values(db, tournament_with_events):
    """Test .only() combined with .values()"""
    with pytest.raises(ValueError):
        await Event.objects.all().only("name").values("name")


@pytest.mark.asyncio
async def test_only_advanced_pk_field(db, tournament_with_events):
    """Test .only() with just the primary key field"""
    tournament = await Tournament.objects.first().only("id")
    assert tournament.id is not None
    with pytest.raises(AttributeError):
        _ = tournament.name


@pytest.mark.asyncio
async def test_only_advanced_empty(db, tournament_with_events):
    """Test .only() with no fields (should raise an error)"""
    with pytest.raises(ValueError):
        await Event.objects.all().only()


@pytest.mark.asyncio
async def test_only_advanced_annotate(db, tournament_with_events):
    tournaments = await Tournament.objects.annotate(event_count=Count("events")).only("name", "event_count")

    assert tournaments[0].name == "Tournament A"
    assert tournaments[0].event_count == 2
    with pytest.raises(AttributeError):
        _ = tournaments[0].desc


@pytest.mark.asyncio
async def test_only_with_non_unique_field_and_aggregate_annotate_does_not_merge_distinct_rows(db):
    """`.only()` is a pure field-subset projection on real model instances - it must never
    change how many rows come back, unlike `.values()`/`.values_list()` (whose whole point when
    combined with an aggregate IS grouping by exactly the fields named). The implicit GROUP BY
    an aggregate annotate() builds used to group by whatever was actually selected - `.only()`
    without the pk in its field list left GROUP BY on just the non-unique selected column(s),
    silently merging two DIFFERENT rows into one aggregated group the moment that column's value
    happened to collide, even though the exact same annotate() with no `.only()` at all
    correctly kept them as two separate rows."""
    t1 = await Tournament.objects.create(name="Dup")
    t2 = await Tournament.objects.create(name="Dup")
    await Event.objects.create(name="E1", tournament=t1)
    await Event.objects.create(name="E2", tournament=t1)
    await Event.objects.create(name="E3", tournament=t2)

    baseline = await Tournament.objects.all().annotate(event_count=Count("events")).order_by("event_count")
    only_result = (
        await Tournament.objects.all().only("name").annotate(event_count=Count("events")).order_by("event_count")
    )

    assert [t.event_count for t in baseline] == [1, 2]
    assert [t.event_count for t in only_result] == [1, 2]


@pytest.mark.asyncio
async def test_only_advanced_nonexistent_field(db, tournament_with_events):
    """Test .only() with a field that doesn't exist"""
    with pytest.raises(FieldError):
        await Event.objects.all().only("nonexistent_field").all()


@pytest.mark.asyncio
async def test_only_advanced_join_in_filter(db, tournament_with_events):
    event = await Event.objects.filter(name="Event 1").only("name").first()
    assert event.name == "Event 1"
    with pytest.raises(AttributeError):
        _ = event.tournament

    event = await Event.objects.filter(tournament__name="Tournament A").only("name").first()
    assert event.name == "Event 1"
    with pytest.raises(AttributeError):
        _ = event.tournament

    event = await Event.objects.filter(tournament__name="Tournament A").only("name", "tournament__name").first()
    assert event.name == "Event 1"
    assert event.tournament.name == "Tournament A"


@pytest.mark.asyncio
async def test_only_advanced_join_in_order_by(db, tournament_with_events):
    events = await Event.objects.all().order_by("name").only("name")
    assert events[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = events[0].tournament

    events = await Event.objects.all().order_by("tournament__name", "name").only("name")
    assert events[0].name == "Event 1"
    with pytest.raises(AttributeError):
        _ = events[0].tournament

    events = await Event.objects.all().order_by("tournament__name", "name").only("name", "tournament__name")
    assert events[0].name == "Event 1"
    assert events[0].tournament.name == "Tournament A"


@pytest.mark.asyncio
async def test_only_advanced_select_related(db, tournament_with_events):
    """Test .only() with .select_related() for basic functionality"""
    event = (
        await Event.objects.filter(name="Event 1")
        .select_related("tournament")
        .only("name", "tournament__name")
        .first()
    )

    assert event.name == "Event 1"
    assert event.tournament.name == "Tournament A"

    with pytest.raises(AttributeError):
        _ = event.id
    assert event.tournament.id == tournament_with_events[0].id


@pytest.mark.asyncio
async def test_only_select_related_resolves_related_source_field_columns(db):
    """.only() naming explicit related-model fields whose own columns are renamed via
    source_field (QuerySet._get_only()'s related-model branch) used to reference the
    related table by FIELD name instead of its real db column - "no such column" on sqlite,
    since SourceFields.fk points at another SourceFields row and every one of its own columns
    is source_field'd."""
    parent = await SourceFields.objects.create(chars="root", blip="root-blip")
    child = await SourceFields.objects.create(chars="child", blip="child-blip", fk=parent)

    obj = (
        await SourceFields.objects.filter(eyedee=child.eyedee)
        .select_related("fk")
        .only("eyedee", "chars", "fk__eyedee", "fk__chars", "fk__blip")
        .first()
    )
    assert obj.chars == "child"
    assert obj.fk.eyedee == parent.eyedee
    assert obj.fk.chars == "root"
    assert obj.fk.blip == "root-blip"


@pytest.mark.asyncio
async def test_select_related_resolves_source_field_columns_without_only(db):
    """Plain select_related() (no .only() at all) on a related model whose columns are all
    renamed via source_field - _join_select_related()'s own related-fields branch used a
    DIFFERENT SELECT-alias convention (db column name) than QuerySet._get_only()'s (field
    name), so Model._init_from_db()'s row-key handling had to (and, before this fix, didn't
    consistently) accept both."""
    parent = await SourceFields.objects.create(chars="root", blip="root-blip")
    child = await SourceFields.objects.create(chars="child", blip="child-blip", fk=parent)

    obj = await SourceFields.objects.filter(eyedee=child.eyedee).select_related("fk").first()
    assert obj.chars == "child"
    assert obj.fk.eyedee == parent.eyedee
    assert obj.fk.chars == "root"
    assert obj.fk.blip == "root-blip"


# ============================================================================
# Combined with prefetch_related, including M2M
#
# .only() is a strict whitelist - "you get exactly what you asked for" - but the local column
# prefetch_related() needs to run its second query (the shadow FK column for a forward relation,
# the PK for a reverse/M2M one) is an internal detail the caller has no reason to know to list.
# Without forcing it into the selection regardless, these crash deep inside the prefetch machinery
# with a confusing AttributeError instead of just working - see _prefetch_map_required_local_fields.
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
async def test_only_with_prefetch_forward_fk(db, tournament_with_events):
    """The forward FK's shadow column (e.g. "tournament_id") isn't in the .only() whitelist at
    all - this must still work, not crash trying to read it off the instance."""
    event = await Event.objects.filter(name="Event 1").only("name").prefetch_related("tournament").first()

    assert event.name == "Event 1"
    assert event.tournament.name == "Tournament A"
    with pytest.raises(AttributeError):
        _ = event.token


@pytest.mark.asyncio
async def test_only_with_prefetch_reverse_fk(db, tournament_with_events):
    """The PK ("event_id"/"name" here it's on Tournament) isn't in the .only() whitelist."""
    tournament = await Tournament.objects.all().only("name").prefetch_related("events").first()

    assert tournament.name == "Tournament A"
    assert {e.name for e in tournament.events} == {"Event 1", "Event 2"}
    with pytest.raises(AttributeError):
        _ = tournament.desc


@pytest.mark.asyncio
async def test_only_with_prefetch_m2m(db, event_with_participants):
    """Event's own PK is "event_id", not "id" - exercises that the fix reads the model's actual
    pk_attr rather than assuming a hardcoded column name."""
    event, team_a, team_b = event_with_participants

    fetched = await Event.objects.all().only("name").prefetch_related("participants").first()

    assert fetched.name == "Event 1"
    assert {team.name for team in fetched.participants} == {"Team A", "Team B"}
    with pytest.raises(AttributeError):
        _ = fetched.token


@pytest.mark.asyncio
async def test_only_with_prefetch_object_form(db, event_with_participants):
    """Same requirement when prefetch_related() is given a Prefetch(...) object instead of a bare
    relation-name string."""
    event, team_a, team_b = event_with_participants

    fetched = (
        await Event.objects.all()
        .only("name")
        .prefetch_related(Prefetch("participants", queryset=Team.objects.filter(name="Team A")))
        .first()
    )

    assert fetched.name == "Event 1"
    assert [team.name for team in fetched.participants] == ["Team A"]


@pytest.mark.asyncio
async def test_only_with_prefetch_call_order_independent(db, tournament_with_events):
    """.prefetch_related() before .only() must behave identically to .only() before
    .prefetch_related() - both are resolved lazily in _make_query()."""
    event = await Event.objects.filter(name="Event 1").prefetch_related("tournament").only("name").first()

    assert event.tournament.name == "Tournament A"


@pytest.mark.asyncio
async def test_only_with_prefetch_does_not_widen_whitelist_for_unrelated_fields(db, tournament_with_events):
    """The fix only force-includes what prefetch_related() actually needs - a field the caller
    neither listed in .only() nor uses for any active prefetch must still be unavailable."""
    event = await Event.objects.filter(name="Event 1").only("name").prefetch_related("tournament").first()

    with pytest.raises(AttributeError):
        _ = event.modified
    with pytest.raises(AttributeError):
        _ = event.token


# ============================================================================
# Further combinations: composite PK, soft_delete_field, optimistic_lock_field, lazy="select",
# VersionedModel
# ============================================================================


@pytest.mark.asyncio
async def test_only_with_composite_pk_whitelists_per_member_field(db):
    """Each pk column is its own separate whitelist entry, not a single "pk" name."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    partial = await CompositePkThing.objects.get(thing_id=1, revision=1).only("name")

    assert partial.name == "A"
    with pytest.raises(AttributeError):
        _ = partial.thing_id
    with pytest.raises(AttributeError):
        _ = partial.revision


@pytest.mark.asyncio
async def test_only_with_composite_pk_save_requires_both_pk_columns(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")

    missing_one = await CompositePkThing.objects.get(thing_id=1, revision=1).only("name", "thing_id")
    with pytest.raises(IncompleteInstanceError):
        await missing_one.save(update_fields=["name"])

    full_pk = await CompositePkThing.objects.get(thing_id=1, revision=1).only("name", "thing_id", "revision")
    full_pk.name = "B"
    await full_pk.save(update_fields=["name"])
    assert (await CompositePkThing.objects.get(thing_id=1, revision=1)).name == "B"


@pytest.mark.asyncio
async def test_only_excluding_soft_delete_field(db):
    """Not including the soft_delete_field itself in .only() is fine on its own - only actually
    calling .delete()/.restore() without the PK loaded is the dangerous case (covered in
    test_soft_delete.py)."""
    obj = await SoftDeleteStandalone.objects.create(name="A")
    partial = await SoftDeleteStandalone.objects.get(pk=obj.pk).only("name")

    assert partial.name == "A"
    with pytest.raises(AttributeError):
        _ = partial.deleted_at


@pytest.mark.asyncio
async def test_only_excluding_optimistic_lock_field_save_raises(db):
    thing = await VersionedThing.objects.create(name="A")
    partial = await VersionedThing.objects.get(pk=thing.pk).only("name")

    with pytest.raises(IncompleteInstanceError):
        partial.name = "B"
        await partial.save(update_fields=["name"])


@pytest.mark.asyncio
async def test_only_with_lazy_select_relation(db):
    """lazy="select" auto-adds a prefetch_related() entry under the hood - the same
    _prefetch_map_required_local_fields() fix that covers explicit .prefetch_related() must cover
    this implicit path too, since it goes through the identical _prefetch_map mechanism."""
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child", parent=parent)

    child = await LazySelectChild.objects.filter(name="Child").only("name").first()

    assert child.parent.name == "Parent"


@pytest.mark.asyncio
async def test_only_with_versioned_model(db):
    doc = await VersionedDocument.objects.create(title="D1")
    partial = await VersionedDocument.objects.get(id=doc.id, version=doc.version).only("title")

    assert partial.title == "D1"
    with pytest.raises(AttributeError):
        _ = partial.id
    with pytest.raises(AttributeError):
        _ = partial.version


@pytest.mark.asyncio
async def test_only_with_versioned_model_incomplete_pk_save_raises(db):
    doc = await VersionedDocument.objects.create(title="D1")
    missing_version = await VersionedDocument.objects.get(id=doc.id, version=doc.version).only("title", "id")

    with pytest.raises(IncompleteInstanceError):
        missing_version.title = "D2"
        await missing_version.save(update_fields=["title"])


@pytest.mark.asyncio
async def test_only_with_select_extra_condition_on_nested_path(db, tournament_with_events):
    """Select(...) on a nested select_related() path, combined with .only() - mirrors the
    top-level case already covered in test_select_related_extra_conditions.py."""
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    fetched = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="leaf")))
        .only("name", "left__name", "left__left__name")
        .first()
    )
    assert fetched.left.left is not None
    assert fetched.left.left.name == "leaf"

    fetched_no_match = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="not-leaf")))
        .only("name", "left__name", "left__left__name")
        .first()
    )
    assert fetched_no_match.left.left is None


@pytest.mark.asyncio
async def test_to_dict_and_dict_on_partial_instance_raise_no_values_fetched(db):
    """Both used to leak a raw AttributeError for the first unloaded field."""
    created = await AutoNowThing.objects.create(name="p")
    partial = await AutoNowThing.objects.filter(id=created.id).only("id", "name").get()

    with pytest.raises(NoValuesFetched, match="AutoNowThing.created_at"):
        partial.to_dict()
    with pytest.raises(NoValuesFetched, match="AutoNowThing.created_at"):
        dict(partial)

    complete = await AutoNowThing.objects.get(id=created.id)
    assert set(complete.to_dict()) == {"id", "name", "created_at", "updated_at"}


@pytest.mark.asyncio
async def test_save_update_fields_on_only_instance_without_auto_now_field(db):
    """An auto_now field is only ever WRITTEN by save() (never read first), so a .only() instance
    that left it unloaded must still save - it used to crash with a raw AttributeError from the
    executor's old-value snapshot. The written value is synced onto the instance afterwards."""
    created = await AutoNowThing.objects.create(name="p")
    partial = await AutoNowThing.objects.filter(id=created.id).only("id", "name").get()
    assert not hasattr(partial, "updated_at")

    partial.name = "changed"
    await partial.save(update_fields=["name"])

    fresh = await AutoNowThing.objects.get(id=created.id)
    assert fresh.name == "changed"
    assert fresh.updated_at > created.updated_at
    assert partial.updated_at == fresh.updated_at


@pytest.mark.asyncio
async def test_save_update_fields_on_defer_instance_without_auto_now_field(db):
    created = await AutoNowThing.objects.create(name="p")
    partial = await AutoNowThing.objects.filter(id=created.id).defer("updated_at", "created_at").get()
    assert not hasattr(partial, "updated_at")

    partial.name = "changed"
    await partial.save(update_fields=["name"])

    fresh = await AutoNowThing.objects.get(id=created.id)
    assert fresh.name == "changed"
    assert fresh.updated_at > created.updated_at


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_save_on_only_instance_restores_version_and_keeps_auto_now_field_unloaded(db):
    await VersionedUniqueAutoNow.objects.create(name="A", tag="tag-a")
    created = await VersionedUniqueAutoNow.objects.create(name="B", tag="tag-b")
    partial = await VersionedUniqueAutoNow.objects.filter(id=created.id).only("id", "tag", "version").get()

    partial.tag = "tag-a"  # collides with the other row's unique tag
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            await partial.save(update_fields=["tag"])

    assert partial.version == 0
    assert not hasattr(partial, "updated_at")

    partial.tag = "tag-c"
    await partial.save(update_fields=["tag"])
    assert partial.version == 1


@pytest.mark.asyncio
async def test_stale_save_on_only_instance_restores_version_and_keeps_auto_now_field_unloaded(db):
    created = await VersionedUniqueAutoNow.objects.create(name="A", tag="tag-a")
    partial = await VersionedUniqueAutoNow.objects.filter(id=created.id).only("id", "name", "version").get()

    created.name = "concurrent"
    await created.save()

    partial.name = "stale"
    with pytest.raises(StaleObjectError):
        await partial.save(update_fields=["name"])

    assert partial.version == 0
    assert not hasattr(partial, "updated_at")
    assert (await VersionedUniqueAutoNow.objects.get(id=created.id)).name == "concurrent"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rolled_back_transaction_restores_version_and_unloads_auto_now_field_on_only_instance(db):
    created = await VersionedUniqueAutoNow.objects.create(name="A", tag="tag-a")
    partial = await VersionedUniqueAutoNow.objects.filter(id=created.id).only("id", "name", "version").get()

    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            partial.name = "changed"
            await partial.save(update_fields=["name"])
            assert partial.version == 1
            assert hasattr(partial, "updated_at")
            raise RuntimeError("roll back")

    assert partial.version == 0
    assert not hasattr(partial, "updated_at")
    assert (await VersionedUniqueAutoNow.objects.get(id=created.id)).name == "A"


@pytest.mark.asyncio
async def test_nested_select_related_keeps_only_fields_of_an_intermediate_hop(db):
    """select_related("a__b") with .only("a__field") keeps a's field and still loads b in full."""
    second_level = await DoubleFK.objects.create(name="second leaf")
    first_level = await DoubleFK.objects.create(name="1st", left=second_level)
    root = await DoubleFK.objects.create(name="root", left=first_level)

    row = (
        await DoubleFK.objects.filter(pk=root.pk).select_related("left__left").only("id", "name", "left__name").first()
    )

    assert row is not None
    assert row.name == "root"
    assert row.left.name == "1st"
    assert row.left.id == first_level.id
    assert row.left.left.name == "second leaf"
    assert row.left.left.id == second_level.id

    nested_only = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left__left")
        .only("id", "left__name", "left__left__name")
        .first()
    )
    assert nested_only is not None
    assert nested_only.left.name == "1st"
    assert nested_only.left.left.name == "second leaf"
    assert nested_only.left.left.id == second_level.id


@pytest.mark.asyncio
async def test_only_related_null_fk_hop_is_none_not_an_empty_instance(db):
    """.only() naming only a deeper hop's field must still yield None for a NULL FK on the way."""
    root = await DoubleFK.objects.create(name="root")

    row = await DoubleFK.objects.filter(pk=root.pk).select_related("left__left").only("id", "left__left__name").first()

    assert row is not None
    assert row.left is None


@pytest.mark.asyncio
async def test_only_related_existing_row_with_all_selected_columns_null_is_loaded(db):
    """A joined row whose .only()-selected columns are all NULL still exists - it's loaded by its pk."""
    middle = await DoubleFK.objects.create(name="middle")
    root = await DoubleFK.objects.create(name="root", left=middle)

    row = await DoubleFK.objects.filter(pk=root.pk).select_related("left").only("id", "left__left_id").first()

    assert row is not None
    assert isinstance(row.left, DoubleFK)
    assert row.left.id == middle.id
    assert row.left.left_id is None


@pytest.mark.asyncio
async def test_only_related_intermediate_hop_is_loaded_by_its_pk(db):
    """A hop .only() selects no field of still gets a real instance carrying its own pk."""
    leaf = await DoubleFK.objects.create(name="leaf")
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)
    lonely_root = await DoubleFK.objects.create(name="lonely")

    rows = (
        await DoubleFK.objects.filter(pk__in=[root.pk, lonely_root.pk]).only("id", "left__left__name").order_by("id")
    )

    assert rows[0].left.id == middle.id
    assert rows[0].left.left.id == leaf.id
    assert rows[0].left.left.name == "leaf"
    assert rows[1].left is None
