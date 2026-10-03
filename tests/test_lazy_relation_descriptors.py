"""Regression tests for MetaInfo._generate_lazy_fk_m2m_fields after consolidating its four
near-identical loops (fk_fields/o2o_fields sharing _generate_lazy_forward_relation_fields,
backward_fk_fields/backward_o2o_fields sharing _generate_lazy_backward_relation_fields) into
two parameterized helpers. Covers all four relation types plus the classic
partial()/closure cross-contamination risk of that type of refactor: two FK fields on the
same model must never resolve to each other's value.
"""

import pytest
import pytest_asyncio

from tests.testmodels import Address, Author, Book, DoubleFK, Event, Tournament


@pytest_asyncio.fixture
async def doublefk_pair(db):
    left_obj = await DoubleFK.objects.create(name="left-target")
    right_obj = await DoubleFK.objects.create(name="right-target")
    middle = await DoubleFK.objects.create(name="middle", left=left_obj, right=right_obj)
    return left_obj, right_obj, middle


@pytest.mark.asyncio
async def test_two_forward_fk_fields_on_same_model_do_not_cross_contaminate(db, doublefk_pair):
    """left and right are both built via _generate_lazy_forward_relation_fields in the same
    loop - each iteration's partial() must close over its own field's _key/relation_field/
    to_field, not share state across fields."""
    left_obj, right_obj, middle = doublefk_pair

    fresh = await DoubleFK.objects.get(pk=middle.pk)
    resolved_left = await fresh.left
    resolved_right = await fresh.right

    assert resolved_left.pk == left_obj.pk
    assert resolved_right.pk == right_obj.pk
    assert resolved_left.pk != resolved_right.pk


@pytest.mark.asyncio
async def test_setting_one_forward_fk_field_does_not_affect_the_other(db, doublefk_pair):
    left_obj, right_obj, middle = doublefk_pair
    other = await DoubleFK.objects.create(name="other")

    fresh = await DoubleFK.objects.get(pk=middle.pk)
    fresh.left = other
    await fresh.save()

    reloaded = await DoubleFK.objects.get(pk=middle.pk)
    assert (await reloaded.left).pk == other.pk
    assert (await reloaded.right).pk == right_obj.pk


@pytest.mark.asyncio
async def test_forward_and_backward_fk_resolve_correctly(db):
    """tournament (forward FK, fk_fields) and events (backward FK, backward_fk_fields) are
    now built by two different helper methods - both must still work."""
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)

    fresh_event = await Event.objects.get(pk=event.pk)
    resolved_tournament = await fresh_event.tournament
    assert resolved_tournament.pk == tournament.pk

    fresh_tournament = await Tournament.objects.get(pk=tournament.pk)
    events = await fresh_tournament.events.all()
    assert [e.pk for e in events] == [event.pk]


@pytest.mark.asyncio
async def test_forward_and_backward_o2o_resolve_correctly(db):
    """address.event (forward O2O, o2o_fields, via _generate_lazy_forward_relation_fields)
    and event.address (backward O2O, backward_o2o_fields, via
    _generate_lazy_backward_relation_fields) are the other two relation types consolidated -
    both must still work."""
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    address = await Address.objects.create(city="Berlin", street="Unter den Linden", event=event)

    fresh_address = await Address.objects.get(pk=address.pk)
    resolved_event = await fresh_address.event
    assert resolved_event.pk == event.pk

    fresh_event = await Event.objects.get(pk=event.pk)
    resolved_address = await fresh_event.address
    assert resolved_address.pk == address.pk


@pytest.mark.asyncio
async def test_backward_o2o_getter_does_not_cache_a_stale_pre_save_result(db):
    """Model._ro2o_getter used to setattr(instance, _key, val) after resolving once, caching
    the QuerySetSingle it built from whatever value the owning side's own pk held AT THAT
    moment. Accessing event.address before event is saved (pk still None) permanently cached
    a queryset built from event_id=None, so even after saving the event and creating its
    Address, event.address kept re-running that same stale, frozen query and returning None
    forever instead of resolving against the instance's current (now-set) pk."""
    tournament = await Tournament.objects.create(name="cup")
    event = Event(name="final", tournament=tournament)
    assert await event.address is None  # accessed before save() - pk is still None here

    await event.save()
    address = await Address.objects.create(city="Berlin", street="Unter den Linden", event=event)

    resolved_address = await event.address
    assert resolved_address is not None
    assert resolved_address.pk == address.pk


@pytest.mark.asyncio
async def test_direct_shadow_column_assignment_invalidates_cached_relation(db):
    """Assigning the shadow "author_id" column directly - a documented, supported pattern - must
    invalidate any already-cached "author" relation object, the same way the "author" property
    setter (_fk_setter) already does. Without this, book.author_id and (await book.author).pk
    disagree after the direct assignment, even though save() (which reads the shadow column
    directly) writes the NEW value correctly."""
    author_one = await Author.objects.create(name="A1")
    author_two = await Author.objects.create(name="A2")
    book = await Book.objects.create(name="B", author=author_one, rating=5.0)

    # Cache "_author" by resolving the relation once, before the direct shadow-column assignment.
    resolved = await book.author
    assert resolved.pk == author_one.pk

    book.author_id = author_two.pk

    refreshed = await book.author
    assert refreshed.pk == author_two.pk
    assert book.author_id == author_two.pk


@pytest.mark.asyncio
async def test_direct_shadow_column_assignment_before_any_cache_read_still_resolves_fresh(db):
    """Same bug, without a prior cache-warming read - the getter's own except-AttributeError
    fallback must still see the NEW shadow-column value, not silently reuse a `_author` that a
    stray earlier attribute (there isn't one here) happened to leave behind."""
    author_one = await Author.objects.create(name="A1")
    author_two = await Author.objects.create(name="A2")
    book = await Book.objects.create(name="B", author=author_one, rating=5.0)

    book.author_id = author_two.pk

    resolved = await book.author
    assert resolved.pk == author_two.pk
