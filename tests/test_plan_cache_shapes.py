"""Query shapes that run on the plan kept for them: each shape called again with other values binds
them on its plan - a hit - and returns what a full build returns (``--verify-plans`` compares the two
on every hit)."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation, hare_test_context
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.plan_cache_shapes_models import Category, Edition, Note, Post, Review, Shelf, Tag, Writer


@pytest_asyncio.fixture(scope="module")
async def database():
    async with hare_test_context(
        ["tests.plan_cache_shapes_models"], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    ) as context:
        yield context


@pytest_asyncio.fixture
async def db(database):
    async with RollbackIsolation(database) as context:
        writers = [await Writer.objects.create(id=number, name=f"writer{number}") for number in (1, 2, 3)]
        tags = [await Tag.objects.create(id=number, name=f"tag{number}") for number in (1, 2, 3)]
        for number in (1, 2, 3, 4):
            post = await Post.objects.create(
                id=number, title=f"post{number}", score=number * 10, writer=writers[number % 3]
            )
            await post.tags.add(*tags[: number % 3 + 1])
        editions = [
            await Edition.objects.create(book_number=book, number=number, title=f"edition{book}.{number}")
            for book, number in ((1, 1), (1, 2), (2, 1))
        ]
        for number, edition in enumerate(editions, 1):
            await Review.objects.create(id=number, stars=number, edition=edition)
        await Note.objects.create(id=1, text="on a post", target=await Post.objects.get(id=1))
        await Note.objects.create(id=2, text="on an edition", target=editions[0])
        await Note.objects.create(id=3, text="on nothing")
        root = await Category.objects.create(id=1, name="root")
        child = await Category.objects.create(id=2, name="child", parent=root)
        await Category.objects.create(id=3, name="leaf", parent=child)
        StatementPlans.forget_all()
        yield context


async def get_ids(queryset: Any) -> list[Any]:
    return [obj.pk async for obj in queryset.order_by("pk")]


async def assert_runs_on_its_plan(
    run: Callable[..., Awaitable[Any]], calls: list[tuple[tuple[Any, ...], Any]], hits: int | None = None
) -> None:
    """Runs a shape with each call's values: each result is the expected one, and every call after
    the first runs on the plan the first one kept - or ``hits`` of them, for a shape keeping a plan
    per something of its values."""
    with patch.object(StatementPlans, "count_hit", side_effect=StatementPlans.count_hit) as spy:
        for values, expected in calls:
            assert await run(*values) == expected
    assert spy.call_count == (len(calls) - 1 if hits is None else hits)


@pytest.mark.asyncio
async def test_a_relation_compared_with_an_object(db):
    writers = {writer.id: writer async for writer in Writer.objects.all()}

    async def run(writer_id):
        return await get_ids(Post.objects.filter(writer=writers[writer_id]))

    await assert_runs_on_its_plan(run, [((1,), [3]), ((2,), [1, 4]), ((3,), [2])])


@pytest.mark.asyncio
async def test_a_relation_in_a_list_of_objects(db):
    writers = {writer.id: writer async for writer in Writer.objects.all()}

    async def run(first, second):
        return await get_ids(Post.objects.filter(writer__in=[writers[first], writers[second]]))

    await assert_runs_on_its_plan(run, [((1, 2), [1, 3, 4]), ((2, 3), [1, 2, 4]), ((3, 1), [2, 3])])


@pytest.mark.asyncio
async def test_a_relation_to_a_composite_key_compared_with_an_object(db):
    editions = {edition.pk: edition async for edition in Edition.objects.all()}

    async def run(key):
        return await get_ids(Review.objects.filter(edition=editions[key]))

    await assert_runs_on_its_plan(run, [(((1, 1),), [1]), (((1, 2),), [2]), (((2, 1),), [3])])


@pytest.mark.asyncio
async def test_a_relation_to_a_composite_key_in_a_list_of_objects(db):
    editions = {edition.pk: edition async for edition in Edition.objects.all()}

    async def run(first, second):
        return await get_ids(Review.objects.filter(edition__in=[editions[first], editions[second]]))

    await assert_runs_on_its_plan(
        run, [(((1, 1), (1, 2)), [1, 2]), (((1, 2), (2, 1)), [2, 3]), (((2, 1), (1, 1)), [1, 3])]
    )


@pytest.mark.asyncio
async def test_a_generic_relation_compared_with_an_object(db):
    posts = {post.id: post async for post in Post.objects.all()}

    async def run(post_id):
        return await get_ids(Note.objects.filter(target=posts[post_id]))

    await assert_runs_on_its_plan(run, [((1,), [1]), ((2,), []), ((1,), [1])])


@pytest.mark.asyncio
async def test_a_generic_relation_in_a_list_of_objects(db):
    posts = {post.id: post async for post in Post.objects.all()}

    async def run(first, second):
        return await get_ids(Note.objects.filter(target__in=[posts[first], posts[second]]))

    await assert_runs_on_its_plan(run, [((1, 2), [1]), ((3, 4), []), ((2, 1), [1])])


@pytest.mark.asyncio
async def test_a_generic_relation_by_the_type_named(db):
    async def run(name):
        return await get_ids(Note.objects.filter(target__type=name))

    # The branch named is part of the plan key.
    await assert_runs_on_its_plan(
        run, [(("post",), [1]), (("edition",), [2]), (("post",), [1]), (("edition",), [2])], hits=2
    )


@pytest.mark.asyncio
async def test_a_generic_relation_by_a_list_of_types(db):
    async def run(names):
        return await get_ids(Note.objects.filter(target__type__in=names))

    await assert_runs_on_its_plan(
        run, [((["post"],), [1]), ((["edition"],), [2]), ((["post"],), [1]), ((["edition"],), [2])], hits=2
    )


@pytest.mark.asyncio
async def test_a_generic_relation_is_null(db):
    async def run(flag):
        return await get_ids(Note.objects.filter(target__isnull=flag))

    await assert_runs_on_its_plan(run, [((True,), [3]), ((True,), [3])])


@pytest.mark.asyncio
async def test_a_relation_compared_with_a_key(db):
    async def run(writer_id):
        return await get_ids(Post.objects.filter(writer=writer_id))

    await assert_runs_on_its_plan(run, [((1,), [3]), ((2,), [1, 4]), ((3,), [2])])


@pytest.mark.asyncio
async def test_a_relation_in_a_list_of_keys(db):
    async def run(first, second):
        return await get_ids(Post.objects.filter(writer__in=[first, second]))

    await assert_runs_on_its_plan(run, [((1, 2), [1, 3, 4]), ((2, 3), [1, 2, 4]), ((3, 1), [2, 3])])


@pytest.mark.asyncio
async def test_a_sql_term_annotation(db):
    from hare.sql import Field, ValueWrapper

    async def run(factor):
        queryset = Post.objects.annotate(scaled=Field("score") * ValueWrapper(factor)).filter(id__lte=2)
        return await queryset.order_by("id").values_list("scaled", flat=True)

    await assert_runs_on_its_plan(run, [((2,), [20, 40]), ((3,), [30, 60]), ((0,), [0, 0])])


@pytest.mark.asyncio
async def test_a_sql_term_as_a_function_argument(db):
    from hare.query.functions import Coalesce
    from hare.sql import ValueWrapper

    async def run(fallback):
        queryset = Post.objects.annotate(value=Coalesce("writer_id", ValueWrapper(fallback))).filter(id=1)
        return await queryset.values_list("value", flat=True)

    await assert_runs_on_its_plan(run, [((7,), [2]), ((8,), [2])])


@pytest.mark.asyncio
async def test_a_sql_term_assigned_by_update(db):
    from hare.sql import Field, ValueWrapper

    async def run(step):
        await Post.objects.filter(id=1).update(score=Field("score") + ValueWrapper(step))
        return await Post.objects.filter(id=1).values_list("score", flat=True)

    await assert_runs_on_its_plan(run, [((1,), [11]), ((2,), [13]), ((5,), [18])], hits=4)


@pytest.mark.asyncio
async def test_a_raw_query(db):
    async def run(score):
        return [
            post.id
            for post in await Post.objects.raw("SELECT * FROM shape_post WHERE score > %s ORDER BY id", [score])
        ]

    await assert_runs_on_its_plan(run, [((15,), [2, 3, 4]), ((35,), [4]), ((0,), [1, 2, 3, 4])])


@pytest.mark.asyncio
async def test_a_raw_query_with_a_column_of_no_field(db):
    async def run(factor):
        posts = await Post.objects.raw(
            "SELECT id, title, score * %s AS scaled FROM shape_post WHERE id <= 2 ORDER BY id", [factor]
        )
        return [post.scaled for post in posts]

    await assert_runs_on_its_plan(run, [((2,), [20, 40]), ((3,), [30, 60])])


@pytest.mark.asyncio
async def test_a_raw_query_keeps_a_plan_per_parameter_type(db):
    async def run(score):
        return [
            post.id
            for post in await Post.objects.raw("SELECT * FROM shape_post WHERE score > %s ORDER BY id", [score])
        ]

    await assert_runs_on_its_plan(
        run, [((15,), [2, 3, 4]), ((15.5,), [2, 3, 4]), ((35,), [4]), ((35.5,), [4])], hits=2
    )


@pytest.mark.asyncio
async def test_a_raw_query_parameter_written_as_a_literal_builds_in_full(db):
    async def run(title):
        return [
            post.id
            for post in await Post.objects.raw("SELECT * FROM shape_post WHERE title <> %s ORDER BY id", [title])
        ]

    # "*" is written into the text, not bound - that call builds in full, the next one is on the plan.
    await assert_runs_on_its_plan(
        run, [(("post1",), [2, 3, 4]), (("*",), [1, 2, 3, 4]), (("post2",), [1, 3, 4])], hits=1
    )


@pytest.mark.asyncio
async def test_exists_over_a_values_query(db):
    from hare.query.expressions import Exists, OuterReference

    async def run(score):
        inner = Post.objects.filter(writer=OuterReference("pk"), score__gt=score).values("id")
        return await get_ids(Writer.objects.filter(Exists(inner)))

    await assert_runs_on_its_plan(run, [((15,), [1, 2, 3]), ((35,), [2]), ((0,), [1, 2, 3])])


@pytest.mark.asyncio
async def test_exists_over_a_slice(db):
    from hare.query.expressions import Exists, OuterReference

    async def run(score):
        inner = Post.objects.filter(writer=OuterReference("pk"), score__gt=score).order_by("id")[:1]
        return await get_ids(Writer.objects.filter(Exists(inner)))

    await assert_runs_on_its_plan(run, [((15,), [1, 2, 3]), ((35,), [2]), ((0,), [1, 2, 3])])


@pytest.mark.asyncio
async def test_exists_over_a_slice_with_an_offset(db):
    from hare.query.expressions import Exists, OuterReference

    async def run(offset):
        inner = Post.objects.filter(writer=OuterReference("pk")).order_by("id")[offset : offset + 1]
        return await get_ids(Writer.objects.filter(Exists(inner)))

    # Writer 2 has posts 1 and 4; every other writer has one post. No offset is another shape.
    await assert_runs_on_its_plan(run, [((1,), [2]), ((2,), []), ((1,), [2])])


@pytest.mark.asyncio
async def test_exists_over_a_queryset_with_its_own_cte(db):
    from hare.query.expressions import CteRows, Exists, OuterReference

    async def run(score):
        inner = Post.objects.with_cte("scored", Post.objects.filter(score__gt=score).values("id")).filter(
            writer=OuterReference("pk"), id__in=CteRows("scored", "id")
        )
        return await get_ids(Writer.objects.filter(Exists(inner)))

    await assert_runs_on_its_plan(run, [((15,), [1, 2, 3]), ((35,), [2]), ((0,), [1, 2, 3])])


@pytest.mark.asyncio
async def test_an_outer_ref_across_a_relation(db):
    from hare.query.expressions import Exists, OuterReference

    async def run(writer_id):
        inner = Writer.objects.filter(name=OuterReference("writer__name"), id__gt=writer_id)
        return await get_ids(Post.objects.filter(Exists(inner)))

    await assert_runs_on_its_plan(run, [((0,), [1, 2, 3, 4]), ((1,), [1, 2, 4]), ((2,), [2])])


@pytest.mark.asyncio
async def test_an_outer_ref_naming_an_annotation_of_the_enclosing_query(db):
    from hare.query.expressions import Exists, F, OuterReference

    async def run(offset, threshold):
        inner = Post.objects.filter(writer_id=OuterReference("shifted"), score__gt=threshold)
        queryset = Writer.objects.annotate(shifted=F("id") + offset).filter(Exists(inner))
        return await get_ids(queryset)

    # Writer 1 has post 3, writer 2 posts 1 and 4, writer 3 post 2.
    await assert_runs_on_its_plan(run, [((0, 0), [1, 2, 3]), ((1, 0), [1, 2]), ((1, 30), [1]), ((-1, 0), [2, 3])])


@pytest.mark.asyncio
async def test_a_filtered_relation(db):
    from hare.query.expressions import FilteredRelation, Q

    async def run(score):
        queryset = Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=score)))
        return await get_ids(queryset.filter(top__id__isnull=False).distinct())

    # Writer 1 has post 3, writer 2 posts 1 and 4, writer 3 post 2.
    await assert_runs_on_its_plan(run, [((15,), [1, 2, 3]), ((25,), [1, 2]), ((35,), [2])])


@pytest.mark.asyncio
async def test_a_filtered_relation_with_a_filter_on_its_path(db):
    from hare.query.expressions import FilteredRelation, Q

    async def run(score, title):
        queryset = Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=score)))
        return await get_ids(queryset.filter(top__title=title))

    await assert_runs_on_its_plan(run, [((15, "post4"), [2]), ((15, "post1"), []), ((5, "post1"), [2])])


@pytest.mark.asyncio
async def test_a_filtered_relation_read_by_values(db):
    from hare.query.expressions import FilteredRelation, Q

    async def run(score):
        queryset = Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=score)))
        return await queryset.order_by("id", "top__id").values_list("id", "top__id")

    await assert_runs_on_its_plan(
        run,
        [((25,), [(1, 3), (2, 4), (3, None)]), ((5,), [(1, 3), (2, 1), (2, 4), (3, 2)])],
    )


@pytest.mark.asyncio
async def test_a_filtered_relation_to_many_to_many(db):
    from hare.query.expressions import FilteredRelation, Q

    async def run(name):
        queryset = Post.objects.annotate(named=FilteredRelation("tags", condition=Q(tags__name=name)))
        return await get_ids(queryset.filter(named__id__isnull=False))

    # Post n has the first n % 3 + 1 tags.
    await assert_runs_on_its_plan(run, [(("tag1",), [1, 2, 3, 4]), (("tag2",), [1, 2, 4]), (("tag3",), [2])])


@pytest.mark.asyncio
async def test_a_filtered_relation_of_a_subquery_named_like_the_enclosing_querys(db):
    from hare.query.expressions import FilteredRelation, Q, Subquery

    async def run(outer_score, inner_score):
        inner = (
            Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=inner_score)))
            .filter(top__id__isnull=False)
            .values("id")
        )
        queryset = Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=outer_score)))
        return await get_ids(queryset.filter(top__id__isnull=False, id__in=Subquery(inner)).distinct())

    # Each condition's values are bound by their origin - the inner one apart from the outer one.
    await assert_runs_on_its_plan(run, [((15, 35), [2]), ((35, 15), [2]), ((25, 0), [1, 2])])


@pytest.mark.asyncio
async def test_a_select_related_condition_on_a_relation_crossed_elsewhere(db):
    from hare.query.expressions import Q
    from hare.query.relation_loading.select import Select

    async def run(name):
        queryset = Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=name)))
        posts = await queryset.order_by("writer__name", "id")
        # Sorted here - where NULL sorts depends on the database.
        return sorted((post.id, post.writer.id if post.writer else None) for post in posts)

    await assert_runs_on_its_plan(
        run,
        [
            (("writer1",), [(1, 2), (2, 3), (3, None), (4, 2)]),
            (("writer2",), [(1, None), (2, 3), (3, None), (4, None)]),
        ],
    )


@pytest.mark.asyncio
async def test_a_recursive_walk(db):
    async def run(start_id):
        return await get_ids(Category.objects.filter(id=start_id).with_recursive("children"))

    await assert_runs_on_its_plan(run, [((1,), [1, 2, 3]), ((2,), [2, 3]), ((3,), [3])])


@pytest.mark.asyncio
async def test_a_recursive_walk_to_the_parents(db):
    async def run(start_id):
        return await get_ids(Category.objects.filter(id=start_id).with_recursive("parent"))

    await assert_runs_on_its_plan(run, [((3,), [1, 2, 3]), ((2,), [1, 2]), ((1,), [1])])


@pytest.mark.asyncio
async def test_a_recursive_walk_of_a_limited_depth(db):
    async def run(start_id, max_depth):
        return await get_ids(Category.objects.filter(id=start_id).with_recursive("children", max_depth=max_depth))

    await assert_runs_on_its_plan(run, [((1, 0), [1]), ((1, 1), [1, 2]), ((1, 2), [1, 2, 3]), ((2, 5), [2, 3])])


@pytest.mark.asyncio
async def test_a_filter_on_a_window_function(db):
    from hare.query.expressions import F, Window
    from hare.query.functions.window import RowNumber

    async def run(rank, offset):
        queryset = Post.objects.annotate(rank=Window(RowNumber(), order_by=[F("score").desc()])).filter(rank__lte=rank)
        return await queryset.order_by("id").values_list("id", flat=True)[offset : offset + 2]

    await assert_runs_on_its_plan(run, [((2, 0), [3, 4]), ((3, 0), [2, 3]), ((4, 1), [2, 3])])


@pytest.mark.asyncio
async def test_a_subquery_filtered_on_a_window_function(db):
    from hare.query.expressions import F, Subquery, Window
    from hare.query.functions.window import RowNumber

    async def run(rank):
        inner = (
            Post.objects.annotate(rank=Window(RowNumber(), order_by=[F("score").desc()]))
            .filter(rank__lte=rank)
            .values("writer_id")
        )
        return await get_ids(Writer.objects.filter(id__in=Subquery(inner)))

    # By score: post 4 (writer 2), post 3 (writer 1), post 2 (writer 3), post 1 (writer 2).
    await assert_runs_on_its_plan(run, [((1,), [2]), ((2,), [1, 2]), ((3,), [1, 2, 3])])


@pytest.mark.asyncio
async def test_a_many_to_many_relation_differing_from_the_outer_row(db):
    from hare.query.expressions import Exists, OuterReference, Q

    async def run(post_id):
        inner = Post.objects.filter(~Q(tags=OuterReference("id")), id=post_id)
        return await get_ids(Tag.objects.filter(Exists(inner)))

    # Post n has the first n % 3 + 1 tags.
    await assert_runs_on_its_plan(run, [((1,), [3]), ((2,), []), ((3,), [2, 3])])


@pytest.mark.asyncio
async def test_a_many_to_many_relation_to_a_composite_key_differing_from_the_outer_row(db):
    from hare.query.expressions import Exists, OuterReference

    shelf = await Shelf.objects.create(id=1, name="first")
    await Shelf.objects.create(id=2, name="empty")
    await shelf.editions.add(await Edition.objects.get(book_number=1, number=1))

    async def run(shelf_name):
        inner = Shelf.objects.filter(editions__not=OuterReference("pk"), name=shelf_name)
        return await get_ids(Edition.objects.filter(Exists(inner)))

    await assert_runs_on_its_plan(
        run, [(("first",), [(1, 2), (2, 1)]), (("empty",), [(1, 1), (1, 2), (2, 1)]), (("first",), [(1, 2), (2, 1)])]
    )


@pytest.mark.asyncio
async def test_distinct_on_fields(db):
    async def run(score):
        queryset = Post.objects.filter(score__gt=score).order_by("writer_id", "-score").distinct("writer_id")
        return await queryset.values_list("id", flat=True)

    # The best post of each writer: writer 1 post 3, writer 2 post 4, writer 3 post 2.
    await assert_runs_on_its_plan(run, [((0,), [3, 4, 2]), ((25,), [3, 4]), ((35,), [4])])


@pytest.mark.asyncio
async def test_an_aggregate_metric_with_a_value_over_a_values_query(db):
    from hare.query.functions import Max, Sum

    async def run(bonus):
        return await Post.objects.values("writer_id").annotate(total=Sum("score")).aggregate(top=Max("total") + bonus)

    # Writer 2 has posts 1 and 4: 10 + 40.
    await assert_runs_on_its_plan(run, [((0,), {"top": 50}), ((5,), {"top": 55})])


@pytest.mark.asyncio
async def test_an_aggregate_metric_with_a_value_over_a_union(db):
    from hare.query.functions import Max

    async def run(bonus):
        queryset = Post.objects.filter(score__gt=10).union(Post.objects.filter(score__lt=30))
        return await queryset.aggregate(top=Max("score") + bonus)

    await assert_runs_on_its_plan(run, [((0,), {"top": 40}), ((2,), {"top": 42})])


@pytest.mark.asyncio
async def test_an_aggregate_metric_with_a_value_over_grouped_rows(db):
    from hare.query.functions import Count, Max

    async def run(score, bonus):
        queryset = Post.objects.filter(score__gt=score).group_by("writer_id").annotate(posts=Count("id"))
        return await queryset.aggregate(top=Max("posts") + bonus)

    await assert_runs_on_its_plan(run, [((0, 0), {"top": 2}), ((15, 1), {"top": 2}), ((35, 1), {"top": 2})])


@pytest.mark.asyncio
async def test_a_values_query_crossing_a_relation_selected_with_a_condition(db):
    from hare.query.expressions import Q
    from hare.query.relation_loading.select import Select

    async def run(name):
        queryset = Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=name)))
        return sorted(await queryset.values_list("id", "writer__name"), key=lambda row: row[0])

    await assert_runs_on_its_plan(
        run,
        [
            (("writer1",), [(1, "writer2"), (2, "writer3"), (3, None), (4, "writer2")]),
            (("writer2",), [(1, None), (2, "writer3"), (3, None), (4, None)]),
        ],
    )


@pytest.mark.asyncio
async def test_a_values_queryset_of_simple_calls_runs_without_a_query(db):
    from hare.query.plans.call_signatures.call_signature_runs import CallSignatureRuns

    async def run(writer_id):
        return await Post.objects.filter(writer_id=writer_id).order_by("id").values_list("id", flat=True)

    with patch.object(
        CallSignatureRuns, "fetch_values_on_plan", side_effect=CallSignatureRuns.fetch_values_on_plan
    ) as spy:
        await assert_runs_on_its_plan(run, [((2,), [1, 4]), ((3,), [2]), ((1,), [3])])
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_values_rows_of_every_shape_of_the_same_calls(db):
    async def run(shape, post_id):
        queryset = Post.objects.filter(id=post_id)
        if shape == "dict":
            rows = await queryset.values("id", "title")
        elif shape == "named":
            rows = [
                tuple(row) + (type(row).__name__,) for row in await queryset.values_list("id", "title", named=True)
            ]
        elif shape == "flat":
            rows = await queryset.values_list("title", flat=True)
        else:
            rows = await queryset.values_list("id", "title")
        return rows

    # Each shape twice, interleaved - a shape reads its own rows from a plan another shape shares.
    for _ in range(2):
        assert await run("tuple", 1) == [(1, "post1")]
        assert await run("named", 2) == [(2, "post2", "Row")]
        assert await run("flat", 3) == ["post3"]
        assert await run("dict", 4) == [{"id": 4, "title": "post4"}]


@pytest.mark.asyncio
async def test_a_single_values_row_of_simple_calls(db):
    async def run(post_id):
        row = await Post.objects.values_list("id", "title", named=True).get(id=post_id)
        first = await Post.objects.filter(id=post_id).values("title").first()
        return row.title, first

    await assert_runs_on_its_plan(
        run, [((1,), ("post1", {"title": "post1"})), ((2,), ("post2", {"title": "post2"}))], hits=2
    )


@pytest.mark.asyncio
async def test_a_subquery_crossing_a_relation_selected_with_a_condition(db):
    from hare.query.expressions import Q, Subquery
    from hare.query.relation_loading.select import Select

    async def run(name):
        inner = (
            Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=name)))
            .filter(writer__name__isnull=False)
            .values("id")
        )
        return await get_ids(Post.objects.filter(id__in=Subquery(inner)))

    # Post 1 and 4 are by writer 2, post 2 by writer 3, post 3 by writer 1.
    await assert_runs_on_its_plan(run, [(("writer1",), [1, 2, 4]), (("writer2",), [2]), (("writer0",), [1, 2, 3, 4])])


@pytest.mark.asyncio
async def test_a_relation_selected_with_a_condition_filtered_ordered_and_read(db):
    from hare.query.expressions import Q
    from hare.query.relation_loading.select import Select

    async def run(name):
        return await (
            Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=name)))
            .filter(writer__name__isnull=False)
            .order_by("writer__name", "id")
            .values_list("id", "writer__name")
        )

    await assert_runs_on_its_plan(
        run,
        [
            (("writer1",), [(1, "writer2"), (4, "writer2"), (2, "writer3")]),
            (("writer2",), [(2, "writer3")]),
        ],
    )


@pytest.mark.asyncio
async def test_one_queryset_built_into_the_query_twice(db):
    async def run(score):
        best = Post.objects.filter(score__gt=score).values("writer_id")
        return await get_ids(Writer.objects.filter(id__in=best).exclude(id__in=best.filter(title="post4")))

    # Writer 1 has post 3, writer 2 posts 1 and 4, writer 3 post 2.
    await assert_runs_on_its_plan(run, [((15,), [1, 3]), ((25,), [1]), ((5,), [1, 3])])


@pytest.mark.asyncio
async def test_a_filtered_relation_ordered_by_its_path(db):
    from hare.query.expressions import FilteredRelation, Q

    async def run(score):
        queryset = Writer.objects.annotate(top=FilteredRelation("posts", condition=Q(posts__score__gt=score)))
        return (
            await queryset.filter(top__id__isnull=False).order_by("top__title", "id").values_list("id", "top__title")
        )

    await assert_runs_on_its_plan(
        run, [((25,), [(1, "post3"), (2, "post4")]), ((15,), [(3, "post2"), (1, "post3"), (2, "post4")])]
    )


@pytest.mark.asyncio
async def test_queries_of_two_models_built_into_another_keep_plans_apart(db):
    async def run(model, name):
        return await get_ids(Post.objects.filter(writer_id__in=model.objects.filter(name=name)))

    # A writer's and a tag's query of one filter are keyed apart - only the third call runs on a plan.
    await assert_runs_on_its_plan(
        run, [((Writer, "writer2"), [1, 4]), ((Tag, "tag3"), [2]), ((Writer, "writer1"), [3])], hits=1
    )


@pytest.mark.asyncio
async def test_a_count_crossing_a_relation_selected_with_a_condition(db):
    from hare.query.expressions import Q
    from hare.query.relation_loading.select import Select

    async def run(lower):
        queryset = Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=lower)))
        return await queryset.filter(writer__name__isnull=False).count()

    await assert_runs_on_its_plan(run, [(("writer1",), 3), (("writer2",), 1), (("",), 4)])


@pytest.mark.asyncio
async def test_a_count_not_crossing_a_relation_selected_with_a_condition(db):
    from hare.query.expressions import Q
    from hare.query.relation_loading.select import Select

    async def run(lower, score):
        queryset = Post.objects.select_related(Select("writer", extra_condition=Q(name__gt=lower)))
        return await queryset.filter(score__gte=score).count()

    # The condition is folded into no JOIN - its value binds nothing.
    await assert_runs_on_its_plan(run, [(("writer1", 20), 3), (("writer2", 40), 1)])


async def get_builds_per_call(
    run: Callable[..., Awaitable[Any]], calls: list[tuple[tuple[Any, ...], Any]]
) -> list[int]:
    """Runs a shape with each call's values, checking each result, and gives the number of queries
    each call built - a query running on the plan of its calls builds none."""
    from hare.query.statements.awaitable_query import AwaitableQuery

    builds = []
    with patch.object(AwaitableQuery, "_make_query", autospec=True, side_effect=AwaitableQuery._make_query) as spy:
        for values, expected in calls:
            count_before = spy.call_count
            assert await run(*values) == expected
            builds.append(spy.call_count - count_before)
    return builds


# Counts the builds a run on a plan saves - plan verification compares after the body.
@pytest.mark.plan_verification_after_test
@pytest.mark.asyncio
async def test_a_grouped_query_filtered_on_its_aggregate_runs_on_the_plan_of_its_calls(db):
    from hare.query.functions import Sum

    async def run(minimum):
        return await (
            Post.objects.annotate(total=Sum("score"))
            .group_by("writer_id")
            .filter(total__gt=minimum)
            .order_by("writer_id")
            .values_list("writer_id", "total")
        )

    builds = await get_builds_per_call(run, [((15,), [(1, 30), (2, 50), (3, 20)]), ((25,), [(1, 30), (2, 50)])])
    assert builds[1:] == [0]


# Counts the builds a run on a plan saves - plan verification compares after the body.
@pytest.mark.plan_verification_after_test
@pytest.mark.asyncio
async def test_aggregate_runs_on_the_plan_of_its_calls(db):
    from hare.query.expressions import Q
    from hare.query.functions import Count, Sum

    async def run(minimum):
        return await Post.objects.filter(score__gte=minimum).aggregate(
            total=Sum("score"), high=Count("id", _filter=Q(score__gt=minimum))
        )

    builds = await get_builds_per_call(run, [((20,), {"total": 90, "high": 2}), ((30,), {"total": 70, "high": 1})])
    assert builds[1:] == [0]


# Counts the builds a run on a plan saves - plan verification compares after the body.
@pytest.mark.plan_verification_after_test
@pytest.mark.asyncio
async def test_a_query_through_a_relation_runs_on_the_plan_of_its_calls(db):
    writers = {writer.id: writer async for writer in Writer.objects.all()}

    async def run(writer_id):
        return sorted(post.id for post in await writers[writer_id].posts.all())

    builds = await get_builds_per_call(run, [((2,), [1, 4]), ((1,), [3]), ((3,), [2])])
    assert builds[1:] == [0, 0]


# Counts the builds a run on a plan saves - plan verification compares after the body.
@pytest.mark.plan_verification_after_test
@pytest.mark.asyncio
async def test_a_prefetching_query_runs_on_the_plan_of_its_calls(db):
    async def run(minimum):
        posts = await Post.objects.filter(score__gte=minimum).prefetch_related("tags").order_by("id")
        return [(post.id, sorted(tag.id for tag in post.tags)) for post in posts]

    builds = await get_builds_per_call(
        run, [((30,), [(3, [1]), (4, [1, 2])]), ((20,), [(2, [1, 2, 3]), (3, [1]), (4, [1, 2])])]
    )
    # The prefetch query is built - the rows' query runs on its plan.
    assert builds[1:] == [1]


@pytest.mark.asyncio
async def test_only_with_a_prefetched_forward_relation_keeps_a_plan_of_its_own(db):
    async def run_prefetching(minimum):
        posts = await Post.objects.filter(score__gte=minimum).only("title").prefetch_related("writer").order_by("id")
        return [(post.title, post.writer.name) for post in posts]

    async def run_plain(minimum):
        return [post.title for post in await Post.objects.filter(score__gte=minimum).only("title").order_by("id")]

    # The plain query keeps the plan the prefetching one selecting the relation's key would not fit.
    assert await run_plain(30) == ["post3", "post4"]
    assert await run_prefetching(30) == [("post3", "writer1"), ("post4", "writer2")]
    assert await run_prefetching(40) == [("post4", "writer2")]


@pytest.mark.asyncio
async def test_a_prefetched_relation_is_iterated_and_queried(db):
    (post,) = await Post.objects.filter(id=2).prefetch_related("tags")

    assert [tag.id async for tag in post.tags] == [1, 2, 3]
    assert sorted(tag.id for tag in await post.tags.filter(id__gte=2)) == [2, 3]
    assert await post.tags.count() == 3
