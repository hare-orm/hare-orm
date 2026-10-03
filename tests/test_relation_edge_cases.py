"""M2M through models with custom FK columns, related_name=False cascades, M2M set() diffing,
mixed-constraint cascade cycles, select_related() validation and filters by partially loaded
instances."""

import os

import pytest
import pytest_asyncio

from hare import prefetch_related_objects
from hare.contrib.test.helpers import hare_test_context
from hare.exceptions import FieldError, ProtectedError, QueryError
from hare.models import NoneAwaitable
from hare.query.expressions import Q
from tests.relation_edge_case_models import (
    EdgeAuthor,
    EdgeBook,
    EdgeChainBottom,
    EdgeChainLower,
    EdgeChainMiddle,
    EdgeChainTop,
    EdgeCourse,
    EdgeCycleFirst,
    EdgeCycleSecond,
    EdgeEnrollment,
    EdgeHiddenCascade,
    EdgeHiddenOneToOne,
    EdgeHiddenProtect,
    EdgeHiddenSetNull,
    EdgePost,
    EdgePostTag,
    EdgeProfile,
    EdgePublisher,
    EdgeSoftEnrollment,
    EdgeSoftTreeNode,
    EdgeStudent,
    EdgeTag,
    EdgeTarget,
    EdgeTreeNode,
)


@pytest_asyncio.fixture
async def db_edges():
    async with hare_test_context(
        modules=["tests.relation_edge_case_models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


async def enrollment_rows() -> list[tuple[int, int, int | None]]:
    return await EdgeEnrollment.objects.all().order_by("id").values_list("student_id", "course_id", "grade")


# M2M through=Model whose FK fields declare source_field=


def test_through_source_field_keys_are_db_columns(db_edges):
    courses_field = EdgeStudent._meta.fields_map["courses"]
    assert courses_field.backward_keys == ("stud_ref",)
    assert courses_field.forward_keys == ("course_ref",)
    students_field = EdgeCourse._meta.fields_map["students"]
    assert students_field.backward_keys == ("course_ref",)
    assert students_field.forward_keys == ("stud_ref",)


@pytest.mark.asyncio
async def test_through_source_field_add_and_read(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    first_course = await EdgeCourse.objects.create(id=1, title="c1")
    second_course = await EdgeCourse.objects.create(id=2, title="c2")
    await student.courses.add(first_course, through_defaults={"grade": 5})
    await second_course.students.add(student, through_defaults={"grade": 3})

    assert await enrollment_rows() == [(1, 1, 5), (1, 2, 3)]
    assert sorted(course.id for course in await student.courses) == [1, 2]
    assert [row.id for row in await first_course.students] == [1]
    assert [row.id for row in await EdgeStudent.objects.filter(courses=first_course)] == [1]
    assert [row.id for row in await EdgeStudent.objects.filter(courses__title="c2")] == [1]
    assert [row.id for row in await EdgeCourse.objects.filter(students__name="s").order_by("id")] == [1, 2]
    assert [row.id for row in await EdgeStudent.objects.exclude(courses__title="c2")] == []
    prefetched = await EdgeStudent.objects.all().prefetch_related("courses")
    assert [sorted(course.id for course in row.courses) for row in prefetched] == [[1, 2]]
    prefetched_backward = await EdgeCourse.objects.all().order_by("id").prefetch_related("students")
    assert [[row.id for row in course.students] for course in prefetched_backward] == [[1], [1]]


@pytest.mark.asyncio
async def test_through_source_field_add_existing_pair_is_ignored(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    course = await EdgeCourse.objects.create(id=1, title="c1")
    await student.courses.add(course, through_defaults={"grade": 5})
    await student.courses.add(course, through_defaults={"grade": 1})
    assert await enrollment_rows() == [(1, 1, 5)]


@pytest.mark.asyncio
async def test_through_source_field_remove_clear_set(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    first_course = await EdgeCourse.objects.create(id=1, title="c1")
    second_course = await EdgeCourse.objects.create(id=2, title="c2")
    third_course = await EdgeCourse.objects.create(id=3, title="c3")
    await student.courses.add(first_course, second_course, through_defaults={"grade": 4})

    await student.courses.remove(first_course)
    assert await enrollment_rows() == [(1, 2, 4)]

    await student.courses.set(second_course, third_course)
    assert await enrollment_rows() == [(1, 2, 4), (1, 3, None)]

    await student.courses.clear()
    assert await enrollment_rows() == []


@pytest.mark.asyncio
async def test_through_source_field_rejects_relation_fields_in_through_defaults(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    course = await EdgeCourse.objects.create(id=1, title="c1")
    other_student = await EdgeStudent.objects.create(id=2, name="other")
    for through_defaults in ({"student": other_student}, {"student_id": 2}, {"course_id": 1}):
        with pytest.raises(QueryError, match="making up the relation"):
            await student.courses.add(course, through_defaults=through_defaults)
    assert await enrollment_rows() == []


@pytest.mark.asyncio
async def test_through_source_field_deleting_a_member_cascades_through_rows(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    first_course = await EdgeCourse.objects.create(id=1, title="c1")
    second_course = await EdgeCourse.objects.create(id=2, title="c2")
    await student.courses.add(first_course, second_course)

    await first_course.delete()
    assert await enrollment_rows() == [(1, 2, None)]
    await student.delete()
    assert await enrollment_rows() == []


# related_name=False


def test_hidden_backward_relation_has_no_accessor(db_edges):
    assert not {name for name in EdgeTarget._meta.fields_map if "hidden" in name}
    assert not hasattr(EdgeTarget, "edge_hidden_cascades")
    assert len(EdgeTarget._meta.hidden_backward_relations) == 4


@pytest.mark.asyncio
async def test_hidden_backward_relation_cascades_on_instance_delete(db_edges):
    target = await EdgeTarget.objects.create(id=1, name="t")
    await EdgeHiddenCascade.objects.create(id=1, target=target)
    await EdgeHiddenSetNull.objects.create(id=1, target=target)
    await EdgeHiddenOneToOne.objects.create(id=1, target=target)

    await target.delete()

    assert await EdgeHiddenCascade.objects.all().count() == 0
    assert await EdgeHiddenOneToOne.objects.all().count() == 0
    assert await EdgeHiddenSetNull.objects.all().values_list("target_id", flat=True) == [None]


@pytest.mark.asyncio
async def test_hidden_backward_relation_cascades_on_queryset_delete(db_edges):
    first_target = await EdgeTarget.objects.create(id=1, name="t1")
    second_target = await EdgeTarget.objects.create(id=2, name="t2")
    await EdgeHiddenCascade.objects.create(id=1, target=first_target)
    await EdgeHiddenCascade.objects.create(id=2, target=second_target)
    await EdgeHiddenSetNull.objects.create(id=1, target=first_target)

    assert await EdgeTarget.objects.filter(id=1).delete() == 1

    assert await EdgeHiddenCascade.objects.all().values_list("id", flat=True) == [2]
    assert await EdgeHiddenSetNull.objects.all().values_list("target_id", flat=True) == [None]


@pytest.mark.asyncio
async def test_hidden_backward_relation_protects(db_edges):
    target = await EdgeTarget.objects.create(id=1, name="t")
    await EdgeHiddenProtect.objects.create(id=1, target=target)

    with pytest.raises(ProtectedError, match="EdgeHiddenProtect.target"):
        await target.delete()
    with pytest.raises(ProtectedError, match="EdgeHiddenProtect.target"):
        await EdgeTarget.objects.filter(id=1).delete()
    assert await EdgeTarget.objects.filter(id=1).exists()


# ManyToManyRelation.set()


async def post_tag_rows() -> list[tuple[int, int, int, str | None]]:
    return await EdgePostTag.objects.all().order_by("id").values_list("id", "tag_id", "weight", "note")


@pytest.mark.asyncio
async def test_set_keeps_through_rows_of_retained_members(db_edges):
    post = await EdgePost.objects.create(id=1, title="p")
    first_tag = await EdgeTag.objects.create(id=1, name="t1")
    second_tag = await EdgeTag.objects.create(id=2, name="t2")
    third_tag = await EdgeTag.objects.create(id=3, name="t3")
    await post.rich_tags.add(first_tag, second_tag, through_defaults={"weight": 5, "note": "keep"})

    await post.rich_tags.set(first_tag, third_tag, through_defaults={"weight": 7})

    assert await post_tag_rows() == [(1, 1, 5, "keep"), (3, 3, 7, None)]
    assert sorted(tag.id for tag in await post.rich_tags) == [1, 3]


@pytest.mark.asyncio
async def test_set_with_clear_recreates_every_row(db_edges):
    post = await EdgePost.objects.create(id=1, title="p")
    first_tag = await EdgeTag.objects.create(id=1, name="t1")
    await post.rich_tags.add(first_tag, through_defaults={"weight": 5})

    await post.rich_tags.set(first_tag, clear=True)

    assert await post_tag_rows() == [(2, 1, 1, None)]


@pytest.mark.asyncio
async def test_set_same_members_writes_nothing(db_edges):
    post = await EdgePost.objects.create(id=1, title="p")
    first_tag = await EdgeTag.objects.create(id=1, name="t1")
    await post.rich_tags.add(first_tag, through_defaults={"weight": 5})
    before = await post_tag_rows()

    await post.rich_tags.set(first_tag)

    assert await post_tag_rows() == before


@pytest.mark.asyncio
async def test_set_without_instances_clears(db_edges):
    post = await EdgePost.objects.create(id=1, title="p")
    await post.tags.add(await EdgeTag.objects.create(id=1, name="t1"))
    await post.tags.set()
    assert await post.tags.all().count() == 0


@pytest.mark.asyncio
async def test_set_on_auto_through_table_and_backward_side(db_edges):
    post = await EdgePost.objects.create(id=1, title="p")
    other_post = await EdgePost.objects.create(id=2, title="o")
    first_tag = await EdgeTag.objects.create(id=1, name="t1")
    second_tag = await EdgeTag.objects.create(id=2, name="t2")
    await post.tags.add(first_tag)

    await post.tags.set(first_tag, second_tag)
    assert sorted(tag.id for tag in await post.tags) == [1, 2]
    await post.tags.set(second_tag)
    assert [tag.id for tag in await post.tags] == [2]

    await second_tag.posts.set(other_post)
    assert [row.id for row in await second_tag.posts] == [2]
    assert [tag.id for tag in await post.tags] == []


@pytest.mark.asyncio
async def test_set_on_soft_delete_through_only_soft_deletes_dropped_members(db_edges):
    student = await EdgeStudent.objects.create(id=1, name="s")
    first_course = await EdgeCourse.objects.create(id=1, title="c1")
    second_course = await EdgeCourse.objects.create(id=2, title="c2")
    await student.soft_courses.add(first_course, second_course, through_defaults={"grade": 4})

    await student.soft_courses.set(first_course, second_course)
    rows = (
        await EdgeSoftEnrollment.objects.filter()
        .include_deleted()
        .order_by("id")
        .values_list("course_id", "grade", "deleted_at")
    )
    assert [(course_id, grade, deleted_at is None) for course_id, grade, deleted_at in rows] == [
        (1, 4, True),
        (2, 4, True),
    ]

    await student.soft_courses.set(second_course)
    rows = (
        await EdgeSoftEnrollment.objects.filter()
        .include_deleted()
        .order_by("id")
        .values_list("course_id", "deleted_at")
    )
    assert [(course_id, deleted_at is None) for course_id, deleted_at in rows] == [(1, False), (2, True)]

    await student.soft_courses.set(first_course, second_course)
    assert sorted(course.id for course in await student.soft_courses) == [1, 2]
    assert await EdgeSoftEnrollment.objects.filter().include_deleted().count() == 3


# Cascade cycles and chains mixing constrained and unconstrained FKs


async def cycle_state() -> tuple[list[int], list[int]]:
    return (
        sorted(await EdgeCycleFirst.objects.all().values_list("id", flat=True)),
        sorted(await EdgeCycleSecond.objects.all().values_list("id", flat=True)),
    )


async def create_cycle(first_id: int = 1) -> EdgeCycleFirst:
    first = await EdgeCycleFirst.objects.create(id=first_id)
    second = await EdgeCycleSecond.objects.create(id=first_id, other=first)
    first.other = second
    await first.save()
    return first


@pytest.mark.asyncio
async def test_mixed_cycle_instance_delete(db_edges):
    first = await create_cycle()
    await first.delete()
    assert await cycle_state() == ([], [])


@pytest.mark.asyncio
async def test_mixed_cycle_queryset_delete(db_edges):
    await create_cycle()
    assert await EdgeCycleFirst.objects.filter(id=1).delete() == 1
    assert await cycle_state() == ([], [])


@pytest.mark.asyncio
async def test_mixed_cycle_queryset_delete_of_several_rows(db_edges):
    await create_cycle(1)
    await EdgeCycleFirst.objects.create(id=2)
    assert await EdgeCycleFirst.objects.all().delete() == 2
    assert await cycle_state() == ([], [])


@pytest.mark.asyncio
async def test_mixed_cycle_delete_from_the_other_side(db_edges):
    first = await create_cycle()
    second = await EdgeCycleSecond.objects.get(id=1)
    await second.delete()
    assert await cycle_state() == ([], [])
    assert first.pk == 1


@pytest.mark.asyncio
async def test_unconstrained_cascade_below_a_constrained_hop_is_applied(db_edges):
    top = await EdgeChainTop.objects.create(id=1)
    middle = await EdgeChainMiddle.objects.create(id=1, top=top)
    lower = await EdgeChainLower.objects.create(id=1, middle=middle)
    await EdgeChainBottom.objects.create(id=1, lower=lower)

    await top.delete()

    assert await EdgeChainMiddle.objects.all().count() == 0
    assert await EdgeChainLower.objects.all().count() == 0
    assert await EdgeChainBottom.objects.all().count() == 0


@pytest.mark.asyncio
async def test_queryset_delete_of_a_tree_whose_rows_cascade_onto_each_other(db_edges):
    root = await EdgeTreeNode.objects.create(id=1)
    child = await EdgeTreeNode.objects.create(id=2, parent=root)
    await EdgeTreeNode.objects.create(id=3, parent=child)

    assert await EdgeTreeNode.objects.all().delete() == 3
    assert await EdgeTreeNode.objects.all().count() == 0


@pytest.mark.asyncio
async def test_queryset_soft_delete_of_a_tree_keeps_each_row_deleted_once(db_edges):
    root = await EdgeSoftTreeNode.objects.create(id=1)
    child = await EdgeSoftTreeNode.objects.create(id=2, parent=root)
    await EdgeSoftTreeNode.objects.create(id=3, parent=child)

    assert await EdgeSoftTreeNode.objects.all().delete() == 3
    deleted_at_by_id = dict(await EdgeSoftTreeNode.objects.filter().include_deleted().values_list("id", "deleted_at"))
    assert all(deleted_at is not None for deleted_at in deleted_at_by_id.values())
    # The cascade of the root soft-deleted its descendants; their own turn in the loop left them alone.
    assert deleted_at_by_id[2] <= deleted_at_by_id[1]
    assert deleted_at_by_id[3] <= deleted_at_by_id[1]


# select_related() validation


@pytest.mark.parametrize(
    ("model", "relation_path"),
    [
        (EdgePublisher, "authors"),
        (EdgePost, "tags"),
        (EdgeTag, "posts"),
        (EdgeAuthor, "books"),
        (EdgeBook, "author__books"),
    ],
)
def test_select_related_rejects_many_valued_relations(db_edges, model, relation_path):
    with pytest.raises(FieldError, match="prefetch_related"):
        model.objects.all().select_related(relation_path)


def test_select_related_rejects_unknown_names_and_plain_fields(db_edges):
    with pytest.raises(FieldError, match="'nonexistent' for models.EdgeAuthor not found"):
        EdgeAuthor.objects.all().select_related("nonexistent")
    with pytest.raises(FieldError, match="'name' on models.EdgeAuthor is not a relation"):
        EdgeAuthor.objects.all().select_related("name")
    with pytest.raises(FieldError, match="'name' on models.EdgeAuthor is not a relation"):
        EdgeBook.objects.all().select_related("author__name")


@pytest.mark.asyncio
async def test_select_related_still_follows_single_valued_relations(db_edges):
    publisher = await EdgePublisher.objects.create(id=1, name="p", code="AA")
    author = await EdgeAuthor.objects.create(id=1, name="a", publisher=publisher)
    await EdgeProfile.objects.create(id=1, bio="b", author=author)
    await EdgeBook.objects.create(id=1, title="b", author=author, publisher=publisher)

    book = await EdgeBook.objects.get(id=1).select_related("author__publisher", "author__profile", "publisher")
    assert book.author.publisher.code == "AA"
    assert book.author.profile.bio == "b"
    assert book.publisher.name == "p"


# Filters/assignments using an instance that didn't load the FK's to_field


@pytest.mark.asyncio
async def test_filter_by_instance_missing_to_field_raises(db_edges):
    publisher = await EdgePublisher.objects.create(id=1, name="p", code="AA")
    await EdgeBook.objects.create(id=1, title="with", publisher=publisher)
    await EdgeBook.objects.create(id=2, title="without")
    partial_publisher = await EdgePublisher.objects.get(id=1).only("id", "name")

    for make_query in (
        lambda: EdgeBook.objects.filter(publisher=partial_publisher),
        lambda: EdgeBook.objects.exclude(publisher=partial_publisher),
        lambda: EdgeBook.objects.filter(Q(publisher=partial_publisher)),
        lambda: EdgeBook.objects.filter(publisher__in=[partial_publisher]),
        lambda: EdgeBook.objects.filter(publisher__not=partial_publisher),
    ):
        with pytest.raises(QueryError, match="EdgePublisher.code"):
            await make_query()
    with pytest.raises(QueryError, match="EdgePublisher.code"):
        await EdgeBook.objects.exclude(publisher=partial_publisher).delete()
    with pytest.raises(QueryError, match="EdgePublisher.code"):
        await EdgeBook.objects.filter(publisher=partial_publisher).update(title="changed")
    assert await EdgeBook.objects.all().order_by("id").values_list("title", flat=True) == ["with", "without"]


@pytest.mark.asyncio
async def test_instance_missing_to_field_in_assignment_and_reverse_access_raises(db_edges):
    publisher = await EdgePublisher.objects.create(id=1, name="p", code="AA")
    await EdgeBook.objects.create(id=1, title="with", publisher=publisher)
    partial_publisher = await EdgePublisher.objects.get(id=1).only("id", "name")

    with pytest.raises(QueryError, match="Assigning 'publisher' needs EdgePublisher.code"):
        EdgeBook(id=2, title="new", publisher=partial_publisher)
    book = await EdgeBook.objects.get(id=1)
    with pytest.raises(QueryError, match="EdgePublisher.code"):
        book.publisher = partial_publisher
    assert book.publisher_id == "AA"
    with pytest.raises(QueryError, match="EdgePublisher.code"):
        await partial_publisher.books_by_code.all()
    with pytest.raises(QueryError, match="Fetching 'books_by_code' needs EdgePublisher.code"):
        await prefetch_related_objects([partial_publisher], "books_by_code")


@pytest.mark.asyncio
async def test_filter_by_fully_loaded_instance_still_works(db_edges):
    publisher = await EdgePublisher.objects.create(id=1, name="p", code="AA")
    await EdgeBook.objects.create(id=1, title="with", publisher=publisher)
    await EdgeBook.objects.create(id=2, title="without")
    loaded_code_publisher = await EdgePublisher.objects.get(id=1).only("id", "code")

    assert [book.title for book in await EdgeBook.objects.filter(publisher=loaded_code_publisher)] == ["with"]
    assert [book.title for book in await EdgeBook.objects.exclude(publisher=loaded_code_publisher)] == ["without"]
    prefetched = await EdgePublisher.objects.all().only("id", "name").prefetch_related("books_by_code")
    assert [[book.title for book in row.books_by_code] for row in prefetched] == [["with"]]


# A relation with no related row


@pytest.mark.asyncio
async def test_empty_relation_is_none_once_loaded_and_awaitable_before(db_edges):
    author = await EdgeAuthor.objects.create(id=1, name="a")
    await EdgeBook.objects.create(id=1, title="with", author=author)
    await EdgeBook.objects.create(id=2, title="without")

    never_loaded = await EdgeBook.objects.get(id=2)
    assert never_loaded.author is NoneAwaitable
    assert await never_loaded.author is None

    assigned = await EdgeBook.objects.get(id=1)
    assigned.author = None
    joined = await EdgeBook.objects.get(id=2).select_related("author")
    fetched = await EdgeBook.objects.get(id=2)
    await prefetch_related_objects([fetched], "author")
    reverse_joined = await EdgeAuthor.objects.get(id=1).select_related("profile")
    for book in (assigned, joined, fetched):
        assert book.author is None
    assert reverse_joined.profile is None
