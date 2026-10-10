"""Related managers of backward foreign keys and many-to-many relations - Django's reads and
writes (add/remove/clear/set with instances or primary key values, get_or_create/
update_or_create), plus composite primary keys through pk ordering, pk__in subqueries, counts
and updates."""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.contrib.test.databases.model_truncation import truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import (
    DoesNotExist,
    IntegrityError,
    QueryError,
    ValidationError,
)
from hare.query.expressions import Exists, OuterReference, Q
from hare.query.functions import Count
from tests.related_manager_models import (
    Bay,
    Crate,
    HiddenItem,
    Item,
    Label,
    OrderedBay,
    Shelf,
    SoftBay,
    SoftBayCrate,
    Sticker,
    Tag,
)


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def related_managers_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.related_manager_models"], db_url=get_test_db_url(), connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def shelves(related_managers_context: Any) -> AsyncGenerator[tuple[Shelf, Shelf]]:
    first = await Shelf.objects.create(id=1, name="first")
    second = await Shelf.objects.create(id=2, name="second")
    await Item.objects.create(id=1, name="a", shelf=first)
    await Item.objects.create(id=2, name="b", shelf=first)
    await Item.objects.create(id=3, name="c", shelf=second)
    await Item.objects.create(id=4, name="loose", shelf=None)
    await Label.objects.create(id=1, name="x", shelf=first)
    await Label.objects.create(id=2, name="y", shelf=second)
    yield first, second
    await truncate_all_models()


@pytest_asyncio.fixture
async def bays(related_managers_context: Any) -> AsyncGenerator[tuple[Bay, Bay, Bay]]:
    first = await Bay.objects.create(row=1, slot=1, name="b11")
    second = await Bay.objects.create(row=1, slot=2, name="b12")
    third = await Bay.objects.create(row=2, slot=1, name="b21")
    await Crate.objects.create(id=1, name="c1", bay=first)
    await Crate.objects.create(id=2, name="c2", bay=first)
    await Crate.objects.create(id=3, name="c3", bay=second)
    await Crate.objects.create(id=4, name="c4", bay=None)
    yield first, second, third
    await truncate_all_models()


async def get_item_ids(shelf: Shelf) -> list[int]:
    return sorted(await shelf.items.values_list("id", flat=True))


async def get_crate_ids(bay: Bay) -> list[int]:
    return sorted(await bay.crates.values_list("id", flat=True))


# Backward foreign key - reads


@pytest.mark.asyncio
async def test_backward_foreign_key_manager_reads_like_a_queryset(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    assert await first.items.count() == 2
    assert await first.items.exists() is True
    assert (await first.items.get(name="b")).id == 2
    assert await first.items.get(does_not_exist_exception=None, name="c") is None
    assert (await first.items.order_by("id").first()).id == 1
    assert (await first.items.order_by("id").last()).id == 2
    assert await first.items.exclude(id=1).values_list("id", flat=True) == [2]
    assert await first.items.order_by("id").values("name") == [{"name": "a"}, {"name": "b"}]
    assert [item.shelf.name for item in await first.items.select_related("shelf")] == ["first", "first"]
    assert sorted(await first.items.distinct().values_list("name", flat=True)) == ["a", "b"]
    only_names = await first.items.only("id", "name").order_by("id")
    assert [item.name for item in only_names] == ["a", "b"]
    with pytest.raises(DoesNotExist):
        await first.items.get(name="c")


@pytest.mark.asyncio
async def test_backward_foreign_key_manager_updates_its_rows(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    assert await first.items.update(name="renamed") == 2
    assert sorted(await Item.objects.all().values_list("name", flat=True)) == ["c", "loose", "renamed", "renamed"]


# Backward foreign key - writes


@pytest.mark.asyncio
async def test_add_points_the_foreign_key_at_the_parent(shelves: tuple[Shelf, Shelf]) -> None:
    first, second = shelves
    moved = await Item.objects.get(id=3)
    loose = await Item.objects.get(id=4)
    await first.items.add(moved, loose)
    assert await get_item_ids(first) == [1, 2, 3, 4]
    assert await get_item_ids(second) == []
    assert moved.shelf_id == 1
    assert loose.shelf_id == 1


@pytest.mark.asyncio
async def test_add_without_bulk_saves_each_instance_and_creates_an_unsaved_one(shelves: tuple[Shelf, Shelf]) -> None:
    _first, second = shelves
    await second.items.add(Item(id=9, name="new"), await Item.objects.get(id=4), bulk=False)
    assert await get_item_ids(second) == [3, 4, 9]


@pytest.mark.asyncio
async def test_add_refuses_unsaved_or_foreign_instances(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    with pytest.raises(QueryError, match="bulk=False"):
        await first.items.add(Item(id=9, name="new"))
    with pytest.raises(ValidationError, match="Expected model type 'Item'"):
        await first.items.add(await Label.objects.get(id=1))  # type: ignore[arg-type]
    with pytest.raises(QueryError):
        await Shelf(id=7, name="unsaved").items.add(await Item.objects.get(id=4))
    assert await Item.objects.filter(shelf=None).count() == 1


@pytest.mark.asyncio
async def test_add_refuses_a_row_its_default_scope_hides(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    hidden = await HiddenItem.objects.create(id=1, name="gone", shelf=None)
    await hidden.delete()
    with pytest.raises(IntegrityError, match="default scope"):
        await first.hidden_items.add(hidden)
    assert (await HiddenItem.objects.all().include_deleted().get(id=1)).shelf_id is None


@pytest.mark.asyncio
async def test_remove_clears_the_foreign_key_of_related_rows_only(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    removed = await Item.objects.get(id=1)
    await first.items.remove(removed)
    assert await get_item_ids(first) == [2]
    assert removed.shelf_id is None
    with pytest.raises(DoesNotExist, match="is not related to"):
        await first.items.remove(await Item.objects.get(id=3))
    assert await Item.objects.get(id=3).values_list("shelf_id", flat=True) == 2


@pytest.mark.asyncio
async def test_clear_empties_the_relation(shelves: tuple[Shelf, Shelf]) -> None:
    first, second = shelves
    await first.items.clear()
    assert await get_item_ids(first) == []
    await second.items.clear(bulk=False)
    assert await get_item_ids(second) == []
    assert await Item.objects.all().count() == 4


@pytest.mark.asyncio
async def test_remove_and_clear_need_a_nullable_foreign_key(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    with pytest.raises(QueryError, match="isn't nullable"):
        await first.labels.remove(await Label.objects.get(id=1))
    with pytest.raises(QueryError, match="isn't nullable"):
        await first.labels.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("given_as", ["arguments", "list", "queryset"])
async def test_set_replaces_the_related_rows(shelves: tuple[Shelf, Shelf], given_as: str) -> None:
    first, _second = shelves
    new_items = [await Item.objects.get(id=2), await Item.objects.get(id=3)]
    if given_as == "arguments":
        await first.items.set(*new_items)
    elif given_as == "list":
        await first.items.set(new_items)
    else:
        await first.items.set(Item.objects.filter(id__in=[2, 3]))
    assert await get_item_ids(first) == [2, 3]
    assert await Item.objects.get(id=1).values_list("shelf_id", flat=True) is None


@pytest.mark.asyncio
async def test_set_with_clear_and_on_a_non_nullable_foreign_key(shelves: tuple[Shelf, Shelf]) -> None:
    first, second = shelves
    await first.items.set([await Item.objects.get(id=3)], clear=True)
    assert await get_item_ids(first) == [3]
    # A non-nullable foreign key only moves the given rows over, like Django.
    await first.labels.set([await Label.objects.get(id=2)])
    assert sorted(await first.labels.values_list("id", flat=True)) == [1, 2]
    assert await second.labels.count() == 0


@pytest.mark.asyncio
async def test_get_or_create_and_update_or_create_through_the_parent(shelves: tuple[Shelf, Shelf]) -> None:
    first, _second = shelves
    existing, created = await first.items.get_or_create(name="a", defaults={"id": 20})
    assert (existing.id, created) == (1, False)
    new_item, created = await first.items.get_or_create(name="z", defaults={"id": 21})
    assert (new_item.id, new_item.shelf_id, created) == (21, 1, True)
    # An item of another shelf doesn't match through this one.
    other, created = await first.items.get_or_create(name="c", defaults={"id": 22})
    assert (other.id, created) == (22, True)

    updated, created = await first.items.update_or_create(name="a", defaults={"name": "aa"})
    assert (updated.id, updated.name, created) == (1, "aa", False)
    created_item, created = await first.items.update_or_create(name="w", defaults={"id": 23})
    assert (created_item.shelf_id, created) == (1, True)
    with pytest.raises(QueryError, match="conflicts with the parent instance"):
        await first.items.get_or_create(name="q", shelf_id=2, defaults={"id": 24})


@pytest.mark.asyncio
async def test_backward_foreign_key_to_a_composite_primary_key(bays: tuple[Bay, Bay, Bay]) -> None:
    first, second, third = bays
    assert await first.crates.count() == 2
    await third.crates.add(await Crate.objects.get(id=3), await Crate.objects.get(id=4))
    assert await get_crate_ids(third) == [3, 4]
    assert await get_crate_ids(second) == []
    await first.crates.remove(await Crate.objects.get(id=1))
    assert await get_crate_ids(first) == [2]
    await first.crates.set([await Crate.objects.get(id=1)])
    assert await get_crate_ids(first) == [1]
    await third.crates.clear()
    assert await get_crate_ids(third) == []
    created, was_created = await second.crates.get_or_create(name="new", defaults={"id": 9})
    assert was_created and (created.bay_row, created.bay_slot) == (1, 2)


# Many-to-many - primary key values and iterables


@pytest.mark.asyncio
async def test_many_to_many_accepts_primary_key_values(shelves: tuple[Shelf, Shelf]) -> None:
    first, second = shelves
    tag = await Tag.objects.create(id=1, name="t")
    await tag.shelves.add(1, second)
    assert sorted(await tag.shelves.values_list("id", flat=True)) == [1, 2]
    await tag.shelves.remove(2)
    assert await tag.shelves.values_list("id", flat=True) == [1]
    with pytest.raises(IntegrityError, match="no Shelf with that primary key"):
        await tag.shelves.add(99)
    with pytest.raises(ValidationError, match="but got 'NoneType'"):
        await tag.shelves.add(None)
    assert [tag.id for tag in await first.tags] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("given_as", ["arguments", "list", "set", "queryset", "values_list", "relation"])
async def test_many_to_many_set_takes_arguments_or_one_iterable(shelves: tuple[Shelf, Shelf], given_as: str) -> None:
    first, second = shelves
    tag = await Tag.objects.create(id=1, name="t")
    other_tag = await Tag.objects.create(id=2, name="u")
    await other_tag.shelves.add(first, second)
    await tag.shelves.add(first)
    members: Any = {
        "arguments": None,
        "list": [first, 2],
        "set": {1, 2},
        "queryset": Shelf.objects.all(),
        "values_list": Shelf.objects.all().values_list("id", flat=True),
        "relation": other_tag.shelves,
    }[given_as]
    if members is None:
        await tag.shelves.set(first, second)
    else:
        await tag.shelves.set(members)
    assert sorted(await tag.shelves.values_list("id", flat=True)) == [1, 2]
    await tag.shelves.set([])
    assert await tag.shelves.count() == 0


@pytest.mark.asyncio
async def test_many_to_many_get_or_create_and_update_or_create(shelves: tuple[Shelf, Shelf]) -> None:
    first, second = shelves
    tag = await Tag.objects.create(id=1, name="t")
    await tag.shelves.add(first)
    member, created = await tag.shelves.get_or_create(name="first")
    assert (member.id, created) == (1, False)
    new_shelf, created = await tag.shelves.get_or_create(name="third", defaults={"id": 3})
    assert (new_shelf.id, created) == (3, True)
    assert sorted(await tag.shelves.values_list("id", flat=True)) == [1, 3]

    updated, created = await tag.shelves.update_or_create(name="first", defaults={"name": "renamed"})
    assert (updated.id, updated.name, created) == (1, "renamed", False)
    assert await Shelf.objects.get(id=1).values_list("name", flat=True) == "renamed"
    # "second" exists but isn't a member - a member is created instead, like Django.
    with pytest.raises(IntegrityError):
        await tag.shelves.get_or_create(name="second", defaults={"id": second.id})


@pytest.mark.asyncio
async def test_many_to_many_to_a_composite_primary_key_by_key_values(bays: tuple[Bay, Bay, Bay]) -> None:
    first, _second, _third = bays
    sticker = await Sticker.objects.create(id=1, name="s")
    await sticker.bays.add((2, 1), first)
    assert sorted(await sticker.bays.values_list("pk", flat=True)) == [(1, 1), (2, 1)]
    await sticker.bays.remove((1, 1))
    assert await sticker.bays.values_list("pk", flat=True) == [(2, 1)]
    await sticker.bays.set([(1, 2), (2, 1)])
    assert sorted(await sticker.bays.values_list("pk", flat=True)) == [(1, 2), (2, 1)]
    await sticker.bays.set(Bay.objects.filter(row=1).values_list("pk", flat=True))
    assert sorted(await sticker.bays.values_list("pk", flat=True)) == [(1, 1), (1, 2)]
    with pytest.raises(ValidationError, match="composite primary key"):
        await sticker.bays.add(5)
    with pytest.raises(IntegrityError, match="no Bay with that primary key"):
        await sticker.bays.add((9, 9))


# Composite primary keys - ordering, iteration, subqueries, counts, updates


@pytest.mark.asyncio
async def test_ordering_by_a_composite_primary_key(bays: tuple[Bay, Bay, Bay]) -> None:
    ascending = [(1, 1), (1, 2), (2, 1)]
    assert [(bay.row, bay.slot) async for bay in Bay.objects.all().iterator(chunk_size=1)] == ascending
    assert [(bay.row, bay.slot) for bay in await Bay.objects.all().order_by("pk")] == ascending
    assert [(bay.row, bay.slot) for bay in await Bay.objects.all().order_by("-pk")] == ascending[::-1]
    assert await Bay.objects.all().order_by("-pk").values_list("pk", flat=True) == ascending[::-1]
    union = Bay.objects.filter(row=1).union(Bay.objects.filter(row=2))
    assert [(bay.row, bay.slot) for bay in await union.order_by("-pk")] == ascending[::-1]
    assert [(bay.row, bay.slot) async for bay in union.iterator(chunk_size=1)] == ascending
    first, last = await Bay.objects.first(), await Bay.objects.last()
    assert ((first.row, first.slot), (last.row, last.slot)) == ((1, 1), (2, 1))
    latest = await Bay.objects.latest("pk")
    assert (latest.row, latest.slot) == (2, 1)


@pytest.mark.asyncio
async def test_meta_ordering_by_a_composite_primary_key(related_managers_context: Any) -> None:
    for row, slot in ((1, 1), (2, 1), (1, 2)):
        await OrderedBay.objects.create(row=row, slot=slot, name=f"o{row}{slot}")
    try:
        assert [(bay.row, bay.slot) for bay in await OrderedBay.objects.all()] == [(2, 1), (1, 2), (1, 1)]
        assert [(bay.row, bay.slot) async for bay in OrderedBay.objects.all().iterator(chunk_size=2)] == [
            (2, 1),
            (1, 2),
            (1, 1),
        ]
    finally:
        await truncate_all_models()


@pytest.mark.asyncio
async def test_pk_in_takes_a_subquery_of_composite_keys(bays: tuple[Bay, Bay, Bay]) -> None:
    row_one = Bay.objects.filter(row=1)
    expected = [(1, 1), (1, 2)]
    assert sorted(await Bay.objects.filter(pk__in=row_one).values_list("pk", flat=True)) == expected
    assert (
        sorted(await Bay.objects.filter(pk__in=row_one.values_list("pk", flat=True)).values_list("pk", flat=True))
        == expected
    )
    union = Bay.objects.filter(row=1, slot=1).union(Bay.objects.filter(row=1, slot=2))
    assert sorted(await Bay.objects.filter(pk__in=union).values_list("pk", flat=True)) == expected
    assert sorted(await Crate.objects.filter(bay__in=row_one).values_list("id", flat=True)) == [1, 2, 3]
    assert sorted(await Crate.objects.filter(bay__not_in=row_one).values_list("id", flat=True)) == []


@pytest.mark.asyncio
async def test_count_over_a_composite_key_skips_a_null_key(bays: tuple[Bay, Bay, Bay]) -> None:
    counts = await Crate.objects.all().aggregate(
        crates=Count("id"), bays=Count("bay", distinct=True), keyed=Count("bay")
    )
    assert counts == {"crates": 4, "bays": 2, "keyed": 3}
    assert await Bay.objects.all().distinct().count() == 3
    per_bay = (
        await Bay.objects.all().annotate(crate_count=Count("crates")).order_by("pk").values_list("name", "crate_count")
    )
    assert per_bay == [("b11", 2), ("b12", 1), ("b21", 0)]


@pytest.mark.asyncio
async def test_update_of_a_foreign_key_to_a_composite_key(bays: tuple[Bay, Bay, Bay]) -> None:
    _first, _second, third = bays
    await Crate.objects.filter(id=1).update(bay=third)
    await Crate.objects.filter(id=2).update(bay=(1, 2))
    await Crate.objects.filter(id=3).update(bay=None)
    assert await Crate.objects.all().order_by("id").values_list("id", "bay") == [
        (1, (2, 1)),
        (2, (1, 2)),
        (3, None),
        (4, None),
    ]


@pytest.mark.asyncio
async def test_relation_to_a_soft_deleted_composite_key_reads_as_missing(related_managers_context: Any) -> None:
    deleted = await SoftBay.objects.create(row=1, slot=1, name="deleted")
    await deleted.delete()
    live = await SoftBay.objects.create(row=1, slot=2, name="live")
    await SoftBayCrate.objects.create(id=1, name="orphan", bay=deleted)
    await SoftBayCrate.objects.create(id=2, name="linked", bay=live)
    await SoftBayCrate.objects.create(id=3, name="unlinked", bay=None)

    async def get_names(queryset: Any) -> list[str]:
        return sorted(await queryset.values_list("name", flat=True))

    try:
        # Like a single-column key: a hidden target is no target for the relation's own lookups...
        assert await get_names(SoftBayCrate.objects.filter(bay__isnull=True)) == ["orphan", "unlinked"]
        assert await get_names(SoftBayCrate.objects.filter(bay=None)) == ["orphan", "unlinked"]
        assert await get_names(SoftBayCrate.objects.filter(bay__isnull=False)) == ["linked"]
        assert await get_names(SoftBayCrate.objects.filter(bay__not_isnull=True)) == ["linked"]
        assert await get_names(SoftBayCrate.objects.exclude(bay__isnull=True)) == ["linked"]
        assert await get_names(SoftBayCrate.objects.filter(~Q(bay__isnull=False))) == ["orphan", "unlinked"]
        assert await SoftBayCrate.objects.filter(bay__isnull=True).count() == 2
        # ...while comparing a key compares the stored columns, and values() reads them.
        assert await get_names(SoftBayCrate.objects.filter(bay=deleted)) == ["orphan"]
        assert await get_names(SoftBayCrate.objects.filter(bay__in=[deleted, live])) == ["linked", "orphan"]
        assert await get_names(SoftBayCrate.objects.filter(bay__not=live)) == ["orphan", "unlinked"]
        assert await SoftBayCrate.objects.all().order_by("id").values_list("bay", flat=True) == [(1, 1), (1, 2), None]
        assert await SoftBayCrate.objects.filter(bay__isnull=True).update(name="detached") == 2
        assert await get_names(SoftBayCrate.objects.all()) == ["detached", "detached", "linked"]
        await deleted.restore()
        assert await SoftBayCrate.objects.filter(bay__isnull=False).count() == 2
    finally:
        await truncate_all_models()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_a_relation_matches_its_ordering(
    shelves: tuple[Shelf, Shelf], bays: tuple[Bay, Bay, Bay]
) -> None:
    first_items = await Item.objects.filter(shelf__not_isnull=True).order_by("shelf", "id").distinct("shelf")
    assert [item.id for item in first_items] == [1, 3]
    first_crates = await Crate.objects.filter(bay__not_isnull=True).order_by("bay", "id").distinct("bay")
    assert [crate.id for crate in first_crates] == [1, 3]
    assert await Bay.objects.all().order_by("-pk").distinct("pk").values_list("pk", flat=True) == [
        (2, 1),
        (1, 2),
        (1, 1),
    ]


@pytest.mark.asyncio
async def test_outer_ref_to_a_composite_key(bays: tuple[Bay, Bay, Bay]) -> None:
    has_crates = Exists(Crate.objects.filter(bay=OuterReference("pk")))
    assert await Bay.objects.filter(has_crates).order_by("pk").values_list("pk", flat=True) == [(1, 1), (1, 2)]
    assert await Bay.objects.exclude(has_crates).values_list("pk", flat=True) == [(2, 1)]
    named_c3 = Exists(Crate.objects.filter(bay=OuterReference("pk"), name="c3"))
    assert await Bay.objects.annotate(has_c3=named_c3).order_by("pk").values_list("has_c3", flat=True) == [
        False,
        True,
        False,
    ]
    # Crates sharing their bay with another crate - the outer row's own relation.
    sharing = Exists(Crate.objects.filter(bay=OuterReference("bay")).exclude(id=OuterReference("id")))
    assert sorted(await Crate.objects.filter(sharing).values_list("id", flat=True)) == [1, 2]


@pytest.mark.asyncio
async def test_update_fields_and_refresh_fields_take_a_relation_name(bays: tuple[Bay, Bay, Bay]) -> None:
    _first, _second, third = bays
    crate = await Crate.objects.get(id=1)
    crate.bay = third
    crate.name = "not saved"
    await crate.save(update_fields=["bay"])
    assert await Crate.objects.get(id=1).values_list("bay", "name") == ((2, 1), "c1")

    stale = await Crate.objects.get(id=3)
    await Crate.objects.filter(id=3).update(bay=third, name="renamed")
    await stale.refresh_from_db(fields=["bay"])
    assert ((stale.bay_row, stale.bay_slot), stale.name) == ((2, 1), "c3")


@pytest.mark.asyncio
async def test_update_or_create_create_defaults_only_apply_to_a_created_row(shelves: tuple[Shelf, Shelf]) -> None:
    """Django 5.0's create_defaults - on the model, a reverse relation and a many-to-many relation."""
    first, _second = shelves
    created_item, created = await Item.objects.update_or_create(
        id=40, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (created_item.name, created) == ("created", True)
    updated_item, created = await Item.objects.update_or_create(
        id=40, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (updated_item.name, created) == ("updated", False)

    reverse_item, created = await first.items.update_or_create(
        id=41, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (reverse_item.name, reverse_item.shelf_id, created) == ("created", first.id, True)
    reverse_item, created = await first.items.update_or_create(
        id=41, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (reverse_item.name, created) == ("updated", False)

    tag = await Tag.objects.create(id=40, name="t")
    member, created = await tag.shelves.update_or_create(
        id=40, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (member.name, created) == ("created", True)
    member, created = await tag.shelves.update_or_create(
        id=40, defaults={"name": "updated"}, create_defaults={"name": "created"}
    )
    assert (member.name, created) == ("updated", False)
    with pytest.raises(QueryError, match="Conflict value"):
        await Item.objects.update_or_create(id=42, create_defaults={"id": 43, "name": "x"})
