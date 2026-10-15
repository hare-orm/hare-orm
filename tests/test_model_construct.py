"""
Tests for Model.construct() classmethod.

This method creates model instances without validation, DB checks, or FK restrictions.
All tests use the ``db`` fixture to ensure Hare is initialized and _meta is fully populated.
"""

import pytest

from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from tests.testmodels import (
    Author,
    Book,
    Dest_null,
    DirtyTrackedThing,
    Event,
    JSONFields,
    O2O_null,
    Reporter,
    Team,
    Tournament,
)

# ---------------------------------------------------------------------------
# Basic data field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_simple_fields(db):
    instance = Tournament.construct(id=1, name="Test")
    assert instance.id == 1
    assert instance.name == "Test"
    assert instance._saved_in_db is False


@pytest.mark.asyncio
async def test_construct_saved_in_db_flag(db):
    instance = Tournament.construct(id=1, name="Test", _saved_in_db=True)
    assert instance._saved_in_db is True


@pytest.mark.asyncio
async def test_construct_defaults_applied(db):
    instance = Tournament.construct(name="Test")
    # id was not provided so should default to None
    assert instance.id is None


@pytest.mark.asyncio
async def test_construct_partial_and_custom_pk_flags(db):
    instance = Tournament.construct(id=1, name="Test", _saved_in_db=True)
    assert instance._partial is False
    assert instance._custom_generated_pk is False
    assert Tournament.construct(name="Test")._custom_generated_pk is False


@pytest.mark.asyncio
async def test_construct_gives_each_instance_its_own_copy_of_a_mutable_default(db):
    first = JSONFields.construct(data={})
    second = JSONFields.construct(data={})

    first.data_default["a"] = 2

    assert second.data_default == {"a": 1}
    assert JSONFields.construct(data={}).data_default == {"a": 1}


@pytest.mark.asyncio
async def test_construct_with_generated_pk_is_saved_with_that_pk(db):
    instance = Tournament.construct(id=4300, name="constructed")
    assert instance._custom_generated_pk is True

    await instance.save()

    assert instance.pk == 4300
    assert (await Tournament.objects.get(pk=4300)).name == "constructed"


@pytest.mark.asyncio
async def test_construct_on_dirty_tracked_model_can_be_saved_as_new_row(db):
    """construct() left _dirty_snapshot unset on a Meta.track_dirty_fields model, so save() failed
    with an AttributeError instead of inserting."""
    instance = DirtyTrackedThing.construct(name="constructed", count=3, nullable=None, data={})
    assert instance.get_dirty_fields()["name"] == (None, "constructed")

    await instance.save()

    assert instance.pk is not None
    assert instance.get_dirty_fields() == {}
    assert (await DirtyTrackedThing.objects.get(pk=instance.pk)).name == "constructed"


@pytest.mark.asyncio
async def test_construct_on_dirty_tracked_model_can_be_saved_as_existing_row(db):
    created = await DirtyTrackedThing.objects.create(name="original", count=1)
    instance = DirtyTrackedThing.construct(
        id=created.id, name="constructed", count=3, nullable=None, data={}, _saved_in_db=True
    )
    # No baseline was read from the DB, so nothing is reported clean until a real snapshot exists.
    assert "name" in instance.get_dirty_fields()

    await instance.save()

    assert instance.get_dirty_fields() == {}
    reloaded = await DirtyTrackedThing.objects.get(pk=created.id)
    assert (reloaded.name, reloaded.count) == ("constructed", 3)


# ---------------------------------------------------------------------------
# Forward FK field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_with_fk_object(db):
    tournament = Tournament.construct(id=1, name="T")
    event = Event.construct(name="E", tournament=tournament)
    assert event.tournament is tournament
    assert event.tournament.name == "T"
    assert event.tournament_id == 1


@pytest.mark.asyncio
async def test_construct_with_fk_none(db):
    event = Event.construct(name="E", tournament=None)
    assert event.tournament_id is None


@pytest.mark.asyncio
async def test_construct_with_fk_unsaved_allowed(db):
    """Unlike __init__, construct() does NOT check _saved_in_db on FK values."""
    tournament = Tournament.construct(name="T")
    # This should NOT raise even though tournament is not saved
    event = Event.construct(name="E", tournament=tournament)
    assert event.tournament is tournament


@pytest.mark.asyncio
async def test_construct_with_source_field_directly(db):
    event = Event.construct(name="E", tournament_id=42)
    assert event.tournament_id == 42


# ---------------------------------------------------------------------------
# Reverse FK (backward FK) field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_backward_fk_as_list(db):
    e1 = Event.construct(name="E1")
    e2 = Event.construct(name="E2")
    tournament = Tournament.construct(id=1, name="T", events=[e1, e2])
    assert len(tournament.events) == 2
    assert [e.name for e in tournament.events] == ["E1", "E2"]


@pytest.mark.asyncio
async def test_construct_backward_fk_is_reverse_relation(db):
    tournament = Tournament.construct(id=1, name="T", events=[Event.construct(name="E")])
    assert isinstance(tournament.events, ReverseRelation)


@pytest.mark.asyncio
async def test_construct_backward_fk_fetched(db):
    tournament = Tournament.construct(id=1, name="T", events=[])
    assert tournament.events._fetched is True


@pytest.mark.asyncio
async def test_construct_backward_fk_empty_list(db):
    tournament = Tournament.construct(id=1, name="T", events=[])
    assert len(tournament.events) == 0
    assert bool(tournament.events) is False


@pytest.mark.asyncio
async def test_construct_backward_fk_contains(db):
    event = Event.construct(name="E1")
    tournament = Tournament.construct(id=1, name="T", events=[event])
    assert event in tournament.events


# ---------------------------------------------------------------------------
# M2M field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_m2m_as_list(db):
    t1 = Team.construct(id=1, name="T1")
    t2 = Team.construct(id=2, name="T2")
    event = Event.construct(name="E", participants=[t1, t2])
    assert len(event.participants) == 2
    assert [t.name for t in event.participants] == ["T1", "T2"]


@pytest.mark.asyncio
async def test_construct_m2m_is_m2m_relation(db):
    event = Event.construct(name="E", participants=[Team.construct(name="T")])
    assert isinstance(event.participants, ManyToManyRelation)


@pytest.mark.asyncio
async def test_construct_m2m_fetched(db):
    event = Event.construct(name="E", participants=[])
    assert event.participants._fetched is True


@pytest.mark.asyncio
async def test_construct_m2m_empty_list(db):
    event = Event.construct(name="E", participants=[])
    assert len(event.participants) == 0


# ---------------------------------------------------------------------------
# Backward O2O field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_backward_o2o(db):
    """Dest_null has backward O2O 'address_null' from O2O_null."""
    o2o_instance = O2O_null.construct(name="test_o2o")
    dest = Dest_null.construct(name="dest", address_null=o2o_instance)
    assert dest.address_null is o2o_instance
    assert dest.address_null.name == "test_o2o"


# ---------------------------------------------------------------------------
# Forward O2O field construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_forward_o2o(db):
    """Address has a forward O2O field 'event' pointing to Event."""
    from tests.testmodels import Address

    event = Event.construct(event_id=10, name="E")
    address = Address.construct(city="NYC", street="5th Ave", event=event)
    assert address.event is event
    assert address.event_id == 10


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_callable_default(db):
    """Event has token field with default=generate_token (callable)."""
    event = Event.construct(name="E")
    assert event.token is not None
    assert isinstance(event.token, str)
    assert len(event.token) > 0


@pytest.mark.asyncio
async def test_construct_none_default(db):
    """Fields without explicit defaults should get None."""
    event = Event.construct(name="E")
    # alias is IntField(null=True) with no explicit default
    assert event.alias is None


@pytest.mark.asyncio
async def test_construct_unprovided_relation_fields_no_default(db):
    """Backward FK/M2M fields not provided should not raise errors.
    They are lazily created by the property getter."""
    tournament = Tournament.construct(name="T")
    # Accessing the events reverse FK should create a lazy ReverseRelation
    events = tournament.events
    assert isinstance(events, ReverseRelation)


# ---------------------------------------------------------------------------
# No validation enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_no_null_validation(db):
    """Unlike __init__, construct() should NOT raise ValueError for null in non-nullable fields."""
    # Event.name is a non-nullable TextField
    event = Event.construct(name=None)
    assert event.name is None


@pytest.mark.asyncio
async def test_construct_no_fk_saved_check(db):
    """construct() should accept unsaved FK objects without raising."""
    tournament = Tournament.construct(name="Unsaved")
    assert tournament._saved_in_db is False
    event = Event.construct(name="E", tournament=tournament)
    assert event.tournament is tournament


# ---------------------------------------------------------------------------
# Without Hare initialization (no db fixture)
# ---------------------------------------------------------------------------


def test_construct_simple_fields_without_init():
    """Simple data fields should work without Hare.init() / db fixture."""
    instance = Tournament.construct(id=1, name="Test")
    assert instance.id == 1
    assert instance.name == "Test"
    assert instance._saved_in_db is False
    assert instance.pk == 1
    assert repr(instance) == "<Tournament: 1>"
    assert str(instance) == "Test"


def test_construct_unknown_kwargs_without_init():
    """Unknown kwargs should work without initialization."""
    instance = Tournament.construct(name="T", custom_attr="hello")
    assert instance.custom_attr == "hello"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_pk_accessible(db):
    assert Tournament.construct(id=5, name="T").pk == 5


@pytest.mark.asyncio
async def test_construct_repr(db):
    instance = Tournament.construct(id=5, name="T")
    assert repr(instance) == "<Tournament: 5>"


@pytest.mark.asyncio
async def test_construct_str(db):
    instance = Tournament.construct(name="MyTournament")
    assert str(instance) == "MyTournament"


@pytest.mark.asyncio
async def test_construct_unknown_kwargs_stored(db):
    """Unknown kwargs should be stored as instance attributes (no validation)."""
    instance = Tournament.construct(name="T", nonexistent=42)
    assert instance.nonexistent == 42


@pytest.mark.asyncio
async def test_construct_fk_source_field_not_overwritten_by_defaults(db):
    """When tournament=obj is passed, tournament_id should not be overwritten to None by defaults."""
    tournament = Tournament.construct(id=7, name="T")
    event = Event.construct(name="E", tournament=tournament)
    assert event.tournament_id == 7


@pytest.mark.asyncio
async def test_construct_nullable_fk(db):
    """Nullable FK field set to None should work."""
    event = Event.construct(name="E", reporter=None)
    assert event.reporter_id is None


@pytest.mark.asyncio
async def test_construct_nullable_fk_with_object(db):
    """Nullable FK field set to an object should work."""
    reporter = Reporter.construct(id=1, name="R")
    event = Event.construct(name="E", reporter=reporter)
    assert event.reporter is reporter
    assert event.reporter_id == 1


@pytest.mark.asyncio
async def test_construct_multiple_fks(db):
    """Event has both tournament (required FK) and reporter (nullable FK)."""
    tournament = Tournament.construct(id=1, name="T")
    reporter = Reporter.construct(id=2, name="R")
    event = Event.construct(name="E", tournament=tournament, reporter=reporter)
    assert event.tournament is tournament
    assert event.tournament_id == 1
    assert event.reporter is reporter
    assert event.reporter_id == 2


@pytest.mark.asyncio
async def test_construct_book_with_author_fk(db):
    """Book has an FK to Author."""
    author = Author.construct(id=1, name="Author")
    book = Book.construct(name="Book", author=author, rating=4.5)
    assert book.author is author
    assert book.author_id == 1
    assert book.rating == 4.5


@pytest.mark.asyncio
async def test_constructor_values_go_through_an_overridden_setattr(db, monkeypatch):
    """Model() sets a new instance's plain columns - given or defaulted - without
    Model.__setattr__; a model overriding __setattr__ still gets every value, and a given
    automatic primary key still marks the instance as carrying its own key."""
    from hare.models import Model

    assignments = []

    def recording_setattr(self, key, value):
        assignments.append(key)
        Model.__setattr__(self, key, value)

    monkeypatch.setattr(Tournament, "__setattr__", recording_setattr)
    Tournament(name="spring", desc="d")
    assert set(Tournament._meta.fields_db_projection) <= set(assignments)
    monkeypatch.undo()

    given_key = Tournament(id=42, name="keyed")
    assert given_key.id == 42
    assert given_key._custom_generated_pk is True
    assert Tournament(name="unkeyed")._custom_generated_pk is False
