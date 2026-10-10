import asyncio

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import ConfigurationError, NoValuesFetched
from hare.query.expressions import Q
from hare.query.relation_loading.select import Select
from tests.testmodels import (
    IntFields,
    LazyJoinedChild,
    LazyJoinedCompositeOwnerChild,
    LazyJoinedParent,
    LazyM2MLeft,
    LazyM2MRight,
    LazyPlainChild,
    LazySelectChild,
    LazySelectMentee,
    LazySelectParent,
    LazySelectSoftDeleteChild,
    LazySelectSoftDeleteParent,
)


@pytest.mark.asyncio
async def test_lazy_joined_hydrates_without_explicit_select_related(db):
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)

    child = await LazyJoinedChild.objects.filter(name="Child").first()
    # a joined relation is a real hydrated instance, accessible synchronously without a further
    # fetch - unlike a non-joined FK, which resolves to a lazy, awaitable-shaped proxy instead.
    assert isinstance(child.parent, LazyJoinedParent)
    assert child.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_joined_matches_explicit_select_related_shape(db):
    """Regression anchor: lazy="joined" must put the instance in exactly the same state
    explicit .select_related(...) would - not some parallel, slightly different mechanism."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)

    auto = await LazyJoinedChild.objects.filter(name="Child").first()
    explicit = await LazyJoinedChild.objects.filter(name="Child").select_related("parent").first()

    assert set(auto.__dict__.keys()) == set(explicit.__dict__.keys())


@pytest.mark.asyncio
async def test_plain_fk_without_lazy_is_unaffected(db):
    """Regression: a model with no lazy= at all must behave exactly as before this feature -
    accessing the relation isn't a real hydrated instance yet, unlike the lazy="joined" case."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyPlainChild.objects.create(name="Plain", parent=parent)

    child = await LazyPlainChild.objects.filter(name="Plain").first()
    assert not isinstance(child.parent, LazyJoinedParent)


@pytest.mark.asyncio
async def test_lazy_select_prefetches_without_explicit_prefetch_related(db):
    """lazy="select" is declared on LazySelectChild.parent (the forward FK) - querying
    LazySelectChild is what auto-prefetches .parent, not the other way around. Backward
    relations don't carry their own lazy= at all (scoped to forward FK/O2O/M2M only, per the
    field-level declaration)."""
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child A", parent=parent)

    fetched = await LazySelectChild.objects.filter(name="Child A").first()
    # a prefetched relation is accessible directly, without raising "not fetched" and without a
    # further await - same observable shape as an explicit .prefetch_related("parent") would give.
    assert fetched.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_select_m2m_prefetches_without_explicit_prefetch_related(db):
    left = await LazyM2MLeft.objects.create(name="Left")
    right_a = await LazyM2MRight.objects.create(name="Right A")
    right_b = await LazyM2MRight.objects.create(name="Right B")
    await left.rights.add(right_a, right_b)

    fetched = await LazyM2MLeft.objects.filter(name="Left").first()
    names = {right.name for right in fetched.rights}
    assert names == {"Right A", "Right B"}


@pytest.mark.asyncio
async def test_defer_related_opts_out_of_joined_default(db):
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)

    child = await LazyJoinedChild.objects.filter(name="Child").defer_related("parent").first()
    assert "parent" not in child.__dict__


@pytest.mark.asyncio
async def test_defer_related_opts_out_of_select_default(db):
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child", parent=parent)

    fetched = await LazySelectParent.objects.filter(name="Parent").defer_related("children").first()
    with pytest.raises(Exception):  # noqa: B017 - NoValuesFetched, accessing an un-prefetched reverse relation
        list(fetched.children)


@pytest.mark.asyncio
async def test_explicit_select_related_still_works_alongside_lazy_default(db):
    """A relation with no lazy= can still be explicitly select_related() on the same query as
    one that's auto-joined via lazy="joined" - the two mechanisms must combine, not conflict."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    child = await LazyJoinedChild.objects.create(name="Child", parent=parent)

    fetched = await LazyJoinedChild.objects.filter(pk=child.pk).select_related("parent").first()
    assert fetched.parent.name == "Parent"


def test_m2m_lazy_joined_rejected():
    with pytest.raises(ConfigurationError):
        from hare import fields
        from hare.models import Model

        class BadM2M(Model):
            others: fields.ManyToManyRelation[LazyM2MRight] = fields.ManyToManyField(
                "models.LazyM2MRight",
                lazy="joined",  # type: ignore[arg-type]
            )


def test_invalid_lazy_value_rejected():
    with pytest.raises(ConfigurationError):
        from hare import fields
        from hare.models import Model

        class BadLazy(Model):
            other: fields.ForeignKeyRelation[LazyJoinedParent] = fields.ForeignKeyField(
                "models.LazyJoinedParent",
                lazy="eager",  # type: ignore[arg-type]
            )


def test_lazy_accepts_relation_load_strategy_enum_member():
    """lazy= is typed as RelationLoadStrategy (a StrEnum, matching OnDelete's own pattern) rather
    than a bare Literal["joined", "select"] - passing the real enum member must work exactly like
    the equivalent plain string, not just be silently tolerated by duck typing."""
    from hare import fields
    from hare.fields import RelationLoadStrategy
    from hare.models import Model

    class LazyEnumField(Model):
        parent: fields.ForeignKeyRelation[LazyJoinedParent] = fields.ForeignKeyField(
            "models.LazyJoinedParent", lazy=RelationLoadStrategy.JOINED, null=True
        )

    assert LazyEnumField._meta.fields_map["parent"].lazy == RelationLoadStrategy.JOINED
    assert LazyEnumField._meta.fields_map["parent"].lazy == "joined"


@pytest.mark.asyncio
async def test_lazy_default_respects_only(db):
    """.only() without an explicit related__field entry must still not select the joined
    relation's columns - lazy="joined" reuses the exact same select_related() machinery, which
    already has this rule."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)

    child = await LazyJoinedChild.objects.filter(name="Child").only("name").first()
    with pytest.raises(AttributeError):
        _ = child.parent


# ============================================================================
# Further combinations: Select(extra_condition=...), .only()/.defer() for lazy="select",
# after_cursor(), soft_delete_field on the related model, composite-PK FK owner
# ============================================================================


@pytest.mark.asyncio
async def test_explicit_select_with_extra_condition_overrides_lazy_joined_default(db):
    """An explicit Select(relation, extra_condition=...) on a lazy="joined" relation isn't just
    "alongside" the default (see test_explicit_select_related_still_works_alongside_lazy_default
    above) - it fully replaces the plain default join with the conditioned one."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    child = await LazyJoinedChild.objects.create(name="Child", parent=parent)

    matching = (
        await LazyJoinedChild.objects.filter(pk=child.pk)
        .select_related(Select("parent", extra_condition=Q(name="Parent")))
        .first()
    )
    assert matching.parent is not None
    assert matching.parent.name == "Parent"

    non_matching = (
        await LazyJoinedChild.objects.filter(pk=child.pk)
        .select_related(Select("parent", extra_condition=Q(name="Someone Else")))
        .first()
    )
    assert non_matching.parent is None


@pytest.mark.asyncio
async def test_lazy_select_respects_only(db):
    """.only() on the base model must not implicitly select the lazy="select" relation's columns
    off the base row - the relation is a separate prefetch query, not a join, so this mirrors
    test_lazy_default_respects_only above but for lazy="select" instead of "joined"."""
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child", parent=parent)

    fetched = await LazySelectChild.objects.filter(name="Child").only("name").first()
    # still auto-prefetched - lazy="select" is a separate query, unaffected by which base columns
    # .only() picked.
    assert fetched.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_select_respects_defer(db):
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child", parent=parent)

    fetched = await LazySelectChild.objects.filter(name="Child").defer("name").first()
    assert fetched.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_select_prefetch_combines_with_after_cursor(db):
    parent = await LazySelectParent.objects.create(name="Parent")
    await LazySelectChild.objects.create(name="Child A", parent=parent)
    b = await LazySelectChild.objects.create(name="Child B", parent=parent)

    fetched = await LazySelectChild.objects.all().order_by("id").after_cursor(b.id - 1).first()
    assert fetched.id == b.id
    # the auto-prefetch still ran, unaffected by the keyset filter on the base query.
    assert fetched.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_select_prefetch_respects_related_soft_delete_auto_filter(db):
    """The related model (LazySelectSoftDeleteParent) has its own soft_delete_field - the
    auto-prefetch triggered by lazy="select" must still apply that model's own default-manager
    filter, exactly like an explicit .prefetch_related("parent") already does."""
    parent = await LazySelectSoftDeleteParent.objects.create(name="Parent")
    child = await LazySelectSoftDeleteChild.objects.create(name="Child", parent=parent)
    await parent.delete()

    fetched = await LazySelectSoftDeleteChild.objects.filter(pk=child.pk).first()
    assert fetched is not None
    assert fetched.parent is None


@pytest.mark.asyncio
async def test_lazy_joined_on_composite_pk_fk_owner(db):
    """lazy="joined" declared on a forward FK owned by a composite-PK model - regression test for
    the get_backward_fk_filters() crash that used to make this combination impossible to even
    initialize (see test_composite_primary_key.py's CompositePkOwningFK tests for the
    non-lazy-specific half of this fix)."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedCompositeOwnerChild.objects.create(a=1, b=1, name="Child", parent=parent)

    fetched = await LazyJoinedCompositeOwnerChild.objects.filter(a=1, b=1).first()
    assert isinstance(fetched.parent, LazyJoinedParent)
    assert fetched.parent.name == "Parent"


@pytest.mark.asyncio
async def test_lazy_relation_field_names_cache_does_not_leak_between_models(db):
    """_apply_lazy_relation_defaults() caches its fk/o2o/m2m field-name union per model
    (JoinResolutionMixin.LAZY_RELATION_FIELD_NAMES_CACHE, keyed by model class) instead of
    rebuilding it on every call - querying a relation-free model must not poison the cache entry
    a relation-bearing model needs (or vice versa), in either query order."""
    await IntFields.objects.filter(intnum=1).first()  # caches an empty union for a model with no relations

    parent = await LazyJoinedParent.objects.create(name="Parent")
    await LazyJoinedChild.objects.create(name="Child", parent=parent)
    child = await LazyJoinedChild.objects.filter(name="Child").first()
    assert isinstance(child.parent, LazyJoinedParent)  # the lazy="joined" default still applied

    # And the reverse order - a relation-bearing model's union must not leak into a model with
    # no relations at all (which would surface as a KeyError on a field name that doesn't exist).
    await IntFields.objects.filter(intnum=1).first()


@pytest.mark.asyncio
async def test_lazy_select_self_referential_fk_loads_one_level_on_a_cycle(db):
    """A self-referential lazy="select" FK is not re-applied to the prefetch query it triggers -
    a cycle in the data used to make the implicit prefetch recurse forever."""
    first = await LazySelectMentee.objects.create(name="first")
    second = await LazySelectMentee.objects.create(name="second", mentor=first)
    await LazySelectMentee.objects.filter(pk=first.pk).update(mentor_id=second.pk)

    fetched = await asyncio.wait_for(LazySelectMentee.objects.get(pk=first.pk), timeout=10)

    assert fetched.mentor.name == "second"
    assert (await fetched.mentor.mentor).name == "first"
    with pytest.raises(NoValuesFetched):
        list(fetched.mentor.skills)


@pytest.mark.asyncio
async def test_lazy_select_self_referential_chain_costs_constant_queries(db):
    mentor = await LazySelectMentee.objects.create(name="root")
    for index in range(20):
        mentor = await LazySelectMentee.objects.create(name=f"m{index}", mentor=mentor)

    async with capture_queries() as counter:
        fetched = await LazySelectMentee.objects.get(pk=mentor.pk)

    # the row itself, its mentor, and the skills through-table lookup
    assert counter.count == 3
    assert fetched.mentor.name == "m18"


@pytest.mark.asyncio
async def test_lazy_select_explicit_nested_prefetch_still_loads_deeper_levels(db):
    root = await LazySelectMentee.objects.create(name="root")
    middle = await LazySelectMentee.objects.create(name="middle", mentor=root)
    leaf = await LazySelectMentee.objects.create(name="leaf", mentor=middle)

    fetched = await LazySelectMentee.objects.filter(pk=leaf.pk).prefetch_related("mentor__mentor").first()

    assert fetched.mentor.mentor.name == "root"


@pytest.mark.asyncio
async def test_refresh_from_db_reloads_lazy_select_fk_and_m2m(db):
    """refresh_from_db() reloads lazy="select" relations the refresh reset, like lazy="joined" ones."""
    old_mentor = await LazySelectMentee.objects.create(name="old")
    new_mentor = await LazySelectMentee.objects.create(name="new")
    skill = await LazyM2MRight.objects.create(name="py")
    mentee = await LazySelectMentee.objects.create(name="mentee", mentor=old_mentor)
    await mentee.skills.add(skill)
    fetched = await LazySelectMentee.objects.get(pk=mentee.pk)
    assert fetched.mentor.name == "old"
    await LazySelectMentee.objects.filter(pk=mentee.pk).update(mentor_id=new_mentor.pk)
    other_skill = await LazyM2MRight.objects.create(name="rust")
    await mentee.skills.add(other_skill)

    await fetched.refresh_from_db()

    assert fetched.mentor.name == "new"
    assert sorted(right.name for right in fetched.skills) == ["py", "rust"]

    await LazySelectMentee.objects.filter(pk=mentee.pk).update(mentor_id=old_mentor.pk)
    await fetched.refresh_from_db(fields=["mentor_id"])

    assert fetched.mentor.name == "old"
