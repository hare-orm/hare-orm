"""A sliced Prefetch queryset takes its slice of each parent's rows - the first N comments of every
post, not of all posts together: a reverse foreign key, a many-to-many relation (a row shared by
several parents falls into each one's slice on its own), composite keys, to_attribute, nested
prefetches, and the single row of a forward relation or a reverse one-to-one."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.query.relation_loading import Prefetch
from tests.models_prefetch_slices import Chapter, Label, Novel, Parcel, Shipment, Writer, WriterProfile


@pytest_asyncio.fixture(scope="module")
async def slices_database():
    async with hare_test_context(
        modules=["tests.models_prefetch_slices"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def library(slices_database):
    async with RollbackIsolation(slices_database):
        anna = await Writer.objects.create(id=1, name="anna")
        boris = await Writer.objects.create(id=2, name="boris")
        await Writer.objects.create(id=3, name="clara")
        await WriterProfile.objects.create(id=1, writer=anna)
        labels = {name: await Label.objects.create(id=index, name=name) for index, name in enumerate("abcd", 1)}
        for novel_id, writer, rating, novel_labels in (
            (1, anna, 5, "abc"),
            (2, anna, 9, "a"),
            (3, anna, 7, "bd"),
            (4, boris, 3, "ab"),
            (5, boris, 8, ""),
        ):
            novel = await Novel.objects.create(id=novel_id, title=f"n{novel_id}", rating=rating, writer=writer)
            await novel.labels.add(*(labels[name] for name in novel_labels))
            for number in (1, 2, 3):
                await Chapter.objects.create(id=novel_id * 10 + number, number=number, novel=novel)
        for store, number, shipment_labels in ((1, 1, "abcd"), (1, 2, "c"), (2, 1, "")):
            shipment = await Shipment.objects.create(store=store, number=number)
            await shipment.labels.add(*(labels[name] for name in shipment_labels))
            for slot, weight in ((1, 30), (2, 10), (3, 20)):
                await Parcel.objects.create(box=store * 10 + number, slot=slot, weight=weight, shipment=shipment)
        yield


def titles(novels) -> list[str]:
    return [novel.title for novel in novels]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_reverse_foreign_key_slice_of_each_parent():
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.order_by("-rating")[:2])
    )
    assert [titles(writer.novels) for writer in writers] == [["n2", "n3"], ["n5", "n4"], []]
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.order_by("-rating")[1:3], to_attribute="runners_up")
    )
    assert [titles(writer.runners_up) for writer in writers] == [["n3", "n1"], ["n4"], []]
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.filter(rating__gte=5).order_by("rating").offset(1))
    )
    assert [titles(writer.novels) for writer in writers] == [["n3", "n2"], [], []]
    # Without an ordering the rows of each parent come by their keys.
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.all()[:1])
    )
    assert [titles(writer.novels) for writer in writers] == [["n1"], ["n4"], []]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_nested_prefetches_of_sliced_rows():
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch(
            "novels",
            queryset=Novel.objects.order_by("-rating")[:1].prefetch_related(
                Prefetch("chapters", queryset=Chapter.objects.order_by("-number")[:2])
            ),
        )
    )
    assert [
        [(novel.title, [chapter.number for chapter in novel.chapters]) for novel in writer.novels]
        for writer in writers
    ] == [
        [("n2", [3, 2])],
        [("n5", [3, 2])],
        [],
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_many_to_many_slice_of_each_owner():
    novels = await Novel.objects.order_by("id").prefetch_related(Prefetch("labels", queryset=Label.objects.all()[:2]))
    # Label's Meta.ordering (name) orders each novel's labels.
    assert [[label.name for label in novel.labels] for novel in novels] == [
        ["a", "b"],
        ["a"],
        ["b", "d"],
        ["a", "b"],
        [],
    ]
    labels = await Label.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.order_by("-rating")[:1], to_attribute="best")
    )
    assert [titles(label.best) for label in labels] == [["n2"], ["n3"], ["n1"], ["n3"]]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_composite_keys():
    shipments = await Shipment.objects.order_by("store", "number").prefetch_related(
        Prefetch("parcels", queryset=Parcel.objects.order_by("weight")[:2]),
        Prefetch("labels", queryset=Label.objects.order_by("-name")[1:3]),
    )
    assert [[parcel.weight for parcel in shipment.parcels] for shipment in shipments] == [[10, 20], [10, 20], [10, 20]]
    assert [[label.name for label in shipment.labels] for shipment in shipments] == [["c", "b"], [], []]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_single_row_relations():
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("profile", queryset=WriterProfile.objects.all()[:1])
    )
    assert [writer.profile is not None for writer in writers] == [True, False, False]
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("profile", queryset=WriterProfile.objects.all()[1:], to_attribute="later_profile")
    )
    assert [writer.later_profile for writer in writers] == [None, None, None]
    novels = await Novel.objects.order_by("id").prefetch_related(Prefetch("writer", queryset=Writer.objects.all()[:5]))
    assert [novel.writer.name for novel in novels] == ["anna", "anna", "anna", "boris", "boris"]
    novels = await Novel.objects.order_by("id").prefetch_related(
        Prefetch("writer", queryset=Writer.objects.all().offset(1), to_attribute="skipped")
    )
    assert [novel.skipped for novel in novels] == [None] * 5
