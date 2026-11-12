"""Query declarations and statements preserve state without sharing mutable containers."""

from copy import copy

import pytest

from hare.query.queryset.specification_copying import SpecificationCopying
from tests.shared_models.testmodels import Tournament


@pytest.mark.asyncio
async def test_copy_preserves_every_clone_slot_value(db):
    qs = Tournament.objects.filter(name="a").order_by("name").distinct()
    cloned = copy(qs)
    for name in SpecificationCopying.get_slot_names(type(qs)):
        if name == "_call_signature":
            # A copy is changed by whoever made it - its calls aren't known (QuerySet.__copy__).
            continue
        original_value = getattr(qs, name)
        assert getattr(cloned, name) == original_value, f"slot {name!r} not preserved by copy"


@pytest.mark.asyncio
async def test_clone_is_independent_of_the_original(db):
    """A clone can change filters and options without changing the source declaration."""
    qs = Tournament.objects.filter(name="a")
    cloned = qs._clone()

    cloned._q_objects.append(Tournament.objects.filter(desc="b")._q_objects[0])
    assert len(cloned._q_objects) == 2
    assert len(qs._q_objects) == 1, "mutating the clone's _q_objects leaked back into the original"

    qs._distinct = True
    assert cloned._distinct is False, "mutating the original after cloning leaked into the clone"


@pytest.mark.asyncio
async def test_copy_works_for_a_structurally_different_sibling_subclass(db):
    """The summary statement retains its state independently of a model-row queryset."""
    count_query = Tournament.objects.filter(name="a").count()
    cloned = copy(count_query)
    for name in SpecificationCopying.get_slot_names(type(count_query)):
        assert getattr(cloned, name) == getattr(count_query, name), f"slot {name!r} not preserved"
    assert cloned is not count_query


@pytest.mark.asyncio
async def test_copy_separates_nested_prefetch_queries(db):
    from hare.query.relation_loading.prefetching.prefetch import Prefetch
    from tests.shared_models.testmodels import Event

    queryset = Tournament.objects.prefetch_related(Prefetch("events", Event.objects.filter(name="first")))
    cloned = copy(queryset)
    cloned._prefetch_queries["events"].append(("other_events", Event.objects.filter(name="second")))
    assert len(queryset._prefetch_queries["events"]) == 1
    assert len(cloned._prefetch_queries["events"]) == 2


@pytest.mark.database_independent
@pytest.mark.parametrize("uses_slots", [False, True])
def test_custom_queryset_copies_mutable_state_through_its_hook(uses_slots):
    from hare.query.queryset import QuerySet

    class TaggedQuerySet(QuerySet):
        __slots__ = ("tags", "__label", "optional") if uses_slots else ("__dict__",)

        def __init__(self, model):
            super().__init__(model)
            self.tags = ["root"]
            self.__label = "declared"

        @property
        def label(self):
            return self.__label

        def copy_extension_state(self, target):
            super().copy_extension_state(target)
            target.tags = list(self.tags)

    queryset = TaggedQuerySet(Tournament)
    branch = queryset.filter(name="first")
    branch.tags.append("branch")
    assert queryset.tags == ["root"]
    assert branch.tags == ["root", "branch"]
    assert branch.label == "declared"
    assert not hasattr(branch, "optional")
    assert type(branch) is TaggedQuerySet


@pytest.mark.database_independent
def test_standard_queryset_copy_omits_execution_origin_and_keeps_prefetch_sets_independent():
    from hare.query.queryset import QuerySet
    from hare.query.queryset.query_specification import QuerySpecification

    queryset = QuerySet(Tournament)
    queryset._plan_origin = object()
    queryset._prefetch_map = {"events": {"participants"}}
    branch = copy(queryset)
    assert not hasattr(branch, "_plan_origin")
    branch._prefetch_map["events"].add("tournament")
    assert queryset._prefetch_map == {"events": {"participants"}}

    # A transfer selecting only the base declaration must not copy QuerySet-only slots.
    target = QuerySet.__new__(QuerySet)
    SpecificationCopying.copy_state(queryset, target, QuerySpecification)
    assert target.model is Tournament
    assert not hasattr(target, "_annotation_descriptions")
    assert not hasattr(target, "_plan_origin")


@pytest.mark.database_independent
@pytest.mark.parametrize("copy_method", ["copy", "keeping_calls", "base_declaration"])
def test_manager_base_identity_does_not_follow_a_copy(copy_method):
    from hare.query.managers.manager import Manager
    from hare.query.queryset import QuerySet
    from hare.query.queryset.query_specification import QuerySpecification

    manager = Manager()
    manager._model = Tournament
    source = manager._get_unchanged_queryset()
    if copy_method == "copy":
        branch = copy(source)
    elif copy_method == "keeping_calls":
        branch = source._clone_keeping_calls()
    else:
        branch = QuerySet.__new__(QuerySet)
        SpecificationCopying.copy_state(source, branch, QuerySpecification)

    assert source._unchanged_base_manager is manager
    assert branch._unchanged_base_manager is None
    assert branch._uses_default_scope is source._uses_default_scope
    branch._annotations["branch"] = object()
    assert source._annotations == {}


@pytest.mark.asyncio
async def test_get_keeps_a_custom_manager_filter_and_queryset_state(db):
    from hare.exceptions import DoesNotExist
    from hare.query.managers.manager import Manager
    from hare.query.queryset import QuerySet

    class TaggedQuerySet(QuerySet):
        __slots__ = ("tag",)

        def __init__(self, model):
            super().__init__(model)
            self.tag = "from manager"

    class FilteredManager(Manager):
        def get_queryset(self):
            return super().get_queryset().filter(name="visible")

    visible = await Tournament.objects.create(name="visible")
    hidden = await Tournament.objects.create(name="hidden")
    manager = FilteredManager(TaggedQuerySet)
    manager._model = Tournament
    base = manager._get_unchanged_queryset()
    branch = base._get_single_queryset(
        (),
        {"id": visible.id},
        does_not_exist_exception=None,
        multiple_objects_returned_exception=None,
    )
    assert isinstance(branch, TaggedQuerySet)
    assert branch.tag == "from manager"
    assert await base.get(id=visible.id) == visible
    with pytest.raises(DoesNotExist):
        await base.get(id=hidden.id)


@pytest.mark.database_independent
def test_copy_metadata_handle_survives_cache_invalidation():
    from hare.query.queryset import QuerySet

    class TaggedQuerySet(QuerySet):
        __slots__ = ("tag",)

    bucket = TaggedQuerySet.copy_plan_bucket
    before = SpecificationCopying.get_slot_copy_plan(TaggedQuerySet)
    assert "tag" in before[0]
    assert bucket is not QuerySet.copy_plan_bucket
    SpecificationCopying.slot_copy_plans.clear()
    assert TaggedQuerySet.copy_plan_bucket is bucket
    assert bucket.get(None) is None
    assert SpecificationCopying.get_slot_copy_plan(TaggedQuerySet) == before
