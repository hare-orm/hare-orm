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
from hare.exceptions import QueryError
from hare.query.expressions import Window
from hare.query.functions.window import RowNumber, Sum
from hare.query.relation_loading import Prefetch, prefetch_related_objects
from hare.sql.analytics import Count
from tests.relations.prefetch_slices_models import Chapter, Label, Novel, Parcel, Shipment, Writer, WriterProfile


@pytest_asyncio.fixture(scope="module")
async def slices_database():
    async with hare_test_context(
        modules=["tests.relations.prefetch_slices_models"],
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


def keys_of_partial(objs, unloaded_name: str) -> list:
    """The keys of partial objects - their unloaded field left unread."""
    assert all(obj._partial and unloaded_name not in vars(obj) for obj in objs)
    return [obj.pk for obj in objs]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_slice_of_partial_rows():
    """The keys of each parent's slice were read by values_list() off the only()/defer() queryset -
    refused: ".values_list() cannot be used with .only()"."""
    novels = await Novel.objects.order_by("id").prefetch_related(
        Prefetch("labels", queryset=Label.objects.only("id").order_by("-name")[1:2], to_attribute="second"),
        Prefetch("labels", queryset=Label.objects.defer("name")[:1], to_attribute="first"),
    )
    assert [keys_of_partial(novel.second, "name") for novel in novels] == [[2], [], [2], [1], []]
    assert [keys_of_partial(novel.first, "name") for novel in novels] == [[1], [1], [2], [1], []]
    shipments = await Shipment.objects.order_by("store", "number").prefetch_related(
        Prefetch("labels", queryset=Label.objects.only("id").order_by("-name")[:2]),
        Prefetch("parcels", queryset=Parcel.objects.defer("weight").order_by("weight")[1:]),
    )
    assert [keys_of_partial(shipment.labels, "name") for shipment in shipments] == [[4, 3], [3], []]
    assert [keys_of_partial(shipment.parcels, "weight") for shipment in shipments] == [
        [(11, 3), (11, 1)],
        [(12, 3), (12, 1)],
        [(21, 3), (21, 1)],
    ]
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.only("id", "rating").order_by("-rating")[:2])
    )
    assert [[novel.rating for novel in writer.novels] for writer in writers] == [[9, 7], [8, 3], []]
    assert [keys_of_partial(writer.novels, "title") for writer in writers] == [[2, 3], [5, 4], []]
    places = Label.objects.only("id").annotate(place=Window(RowNumber(), order_by=["-name"]))
    novels = await Novel.objects.order_by("id").prefetch_related(Prefetch("labels", queryset=places[:1]))
    assert [[(label.pk, label.place) for label in novel.labels] for novel in novels] == [
        [(1, 4)],
        [(1, 4)],
        [(2, 3)],
        [(1, 4)],
        [],
    ]
    assert [keys_of_partial(novel.labels, "name") for novel in novels] == [[1], [1], [2], [1], []]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_slice_of_rows_with_their_related_rows_joined():
    """The keys of each parent's slice were read by values()/values_list() off the select_related()
    queryset - refused: ".values_list() cannot be used with select_related('writer')"."""
    labels = await Label.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=Novel.objects.select_related("writer").order_by("-rating")[:1])
    )
    assert [[(novel.title, novel.writer.name) for novel in label.novels] for label in labels] == [
        [("n2", "anna")],
        [("n3", "anna")],
        [("n1", "anna")],
        [("n3", "anna")],
    ]
    novels = await Novel.objects.order_by("id").prefetch_related(
        Prefetch("chapters", queryset=Chapter.objects.select_related("novel").order_by("-number")[1:])
    )
    assert [[(chapter.number, chapter.novel.title) for chapter in novel.chapters] for novel in novels] == [
        [(2, f"n{novel_id}"), (1, f"n{novel_id}")] for novel_id in range(1, 6)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_slice_of_rows_a_to_many_filter_repeats():
    """n1 and n4 have labels a and b, so the filter repeats them. Numbered before DISTINCT, a distinct
    slice skipped rows (anna's [:2] was [n1]); without distinct, a key picked every repeat of its row
    (anna's [:1] was [n1, n1])."""
    labelled = Novel.objects.filter(labels__name__in=["a", "b"]).order_by("id")
    distinct = labelled.distinct()

    async def get_writer_titles(queryset):
        writers = await Writer.objects.order_by("id").prefetch_related(Prefetch("novels", queryset=queryset))
        return [titles(writer.novels) for writer in writers]

    assert await get_writer_titles(distinct[:2]) == [["n1", "n2"], ["n4"], []]
    assert await get_writer_titles(distinct[1:2]) == [["n2"], [], []]
    assert await get_writer_titles(labelled[:1]) == [["n1"], ["n4"], []]
    assert await get_writer_titles(labelled[:2]) == [["n1", "n1"], ["n4", "n4"], []]
    assert await get_writer_titles(labelled[1:3]) == [["n1", "n2"], ["n4"], []]

    async def get_label_titles(queryset):
        labels = await Label.objects.order_by("id").prefetch_related(Prefetch("novels", queryset=queryset))
        return [titles(label.novels) for label in labels]

    assert await get_label_titles(distinct[:2]) == [["n1", "n2"], ["n1", "n3"], ["n1"], ["n3"]]
    assert await get_label_titles(labelled[:2]) == [["n1", "n1"], ["n1", "n1"], ["n1", "n1"], ["n3"]]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_prefetch_through_a_relation_only_loads_reads_the_keys_it_needs():
    """only() left out the key a nested prefetch reads on the relation it loads - NoValuesFetched; a
    prefetch of a partial object missing its key raised NoValuesFetched, not QueryError."""
    chapters = (
        await Chapter.objects.filter(number=1)
        .order_by("id")
        .only("number", "novel__title")
        .prefetch_related("novel__writer", "novel__labels")
    )
    assert [(chapter.novel.title, chapter.novel.writer.name) for chapter in chapters] == [
        ("n1", "anna"),
        ("n2", "anna"),
        ("n3", "anna"),
        ("n4", "boris"),
        ("n5", "boris"),
    ]
    assert [[label.name for label in chapter.novel.labels] for chapter in chapters] == [
        ["a", "b", "c"],
        ["a"],
        ["b", "d"],
        ["a", "b"],
        [],
    ]
    partial_chapter = await Chapter.objects.only("number").get(id=11)
    with pytest.raises(QueryError, match="Fetching 'novel'"):
        await prefetch_related_objects([partial_chapter], "novel")
    partial_novel = await Novel.objects.only("title").get(id=1)
    with pytest.raises(QueryError, match="Fetching 'labels'"):
        await prefetch_related_objects([partial_novel], "labels")


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_deep_only_keeps_the_keys_of_intermediate_prefetches():
    """An explicit prefetch can replace a relation loaded by a deeper only() path."""
    chapters = (
        await Chapter.objects.filter(number=1)
        .order_by("id")
        .only("number", "novel__writer__name")
        .prefetch_related(Prefetch("novel__writer", queryset=Writer.objects.filter(name="anna")))
    )
    assert [chapter.novel.writer.name if chapter.novel.writer is not None else None for chapter in chapters] == [
        "anna",
        "anna",
        "anna",
        None,
        None,
    ]
    assert all(chapter.novel._partial and "title" not in vars(chapter.novel) for chapter in chapters)


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
@pytest.mark.parametrize(
    ("model", "selected_field", "path", "relation"),
    [
        (Chapter, "number", "novel__writer", "novel"),
        (Parcel, "weight", "shipment__labels", "shipment"),
    ],
)
async def test_a_nested_prefetch_requires_loaded_single_and_composite_relation_keys(
    model, selected_field, path, relation
):
    partial = await model.objects.only(selected_field).first()
    with pytest.raises(QueryError, match=f"Fetching '{relation}'"):
        await prefetch_related_objects([partial], path)


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


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_window_reads_every_row_of_the_parents():
    # The window is computed as without the slice - over the rows of every parent.
    places = Novel.objects.annotate(place=Window(RowNumber(), order_by=["-rating"]), total=Window(Sum("rating")))
    writers = await Writer.objects.order_by("id").prefetch_related(
        Prefetch("novels", queryset=places.order_by("-rating")[1:2])
    )
    assert [[(novel.title, novel.place, novel.total) for novel in writer.novels] for writer in writers] == [
        [("n3", 3, 32)],
        [("n4", 5, 32)],
        [],
    ]
    labels = Label.objects.annotate(place=Window(RowNumber(), order_by=["-name"]))
    novels = await Novel.objects.order_by("id").prefetch_related(
        Prefetch("labels", queryset=labels[:1], to_attribute="first_label")
    )
    assert [[(label.name, label.place) for label in novel.first_label] for novel in novels] == [
        [("a", 4)],
        [("a", 4)],
        [("b", 3)],
        [("a", 4)],
        [],
    ]
    parcels = Parcel.objects.annotate(total=Window(Sum("weight"))).order_by("weight")
    shipments = await Shipment.objects.order_by("store", "number").prefetch_related(
        Prefetch("parcels", queryset=parcels[:1])
    )
    assert [[(parcel.weight, parcel.total) for parcel in shipment.parcels] for shipment in shipments] == [
        [(10, 180)],
        [(10, 180)],
        [(10, 180)],
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
async def test_a_sql_term_window_reads_every_row_before_slicing():
    novels = Novel.objects.annotate(total=Count("*").over()).order_by("id")
    assert [novel.total for novel in await novels] == [5] * 5
    writers = await Writer.objects.order_by("id").prefetch_related(Prefetch("novels", queryset=novels[:1]))
    assert [[(novel.id, novel.total) for novel in writer.novels] for writer in writers] == [
        [(1, 5)],
        [(4, 5)],
        [],
    ]


@pytest.mark.asyncio
async def test_a_slice_of_many_parents_of_a_composite_key(slices_database):
    """The parents' composite keys are OR-ed in one query - SQLite refused 600 parents of a sliced
    prefetch: "Expression tree is too large (maximum depth 1000)"."""
    async with RollbackIsolation(slices_database):
        created = await Shipment.objects.bulk_create([Shipment(store=9, number=number) for number in range(600)])
        await Parcel.objects.bulk_create(
            [
                Parcel(box=1000 + shipment.number, slot=slot, weight=slot, shipment=shipment)
                for shipment in created
                for slot in (1, 2, 3)
            ]
        )
        labels = [await Label.objects.create(id=900 + index, name=f"l{index}") for index in range(3)]
        for shipment in await Shipment.objects.filter(store=9, number__lt=3):
            await shipment.labels.add(*labels)
        shipments = (
            await Shipment.objects.filter(store=9)
            .order_by("number")
            .prefetch_related(
                Prefetch("parcels", queryset=Parcel.objects.order_by("-weight")[:2]),
                Prefetch("labels", queryset=Label.objects.order_by("name")[:1]),
            )
        )
    assert len(shipments) == 600
    assert all([parcel.weight for parcel in shipment.parcels] == [3, 2] for shipment in shipments)
    assert [[label.name for label in shipment.labels] for shipment in shipments[:4]] == [["l0"], ["l0"], ["l0"], []]
