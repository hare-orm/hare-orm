"""Filters, summaries and set operations preserve selected identities and row multiplicities."""

from collections import Counter
from operator import attrgetter, itemgetter

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries, hare_test_context
from hare.exceptions import NoValuesFetched, OperationalError, QueryError, UnSupportedError
from hare.query.expressions import Exists, OuterReference, Q, Subquery
from hare.query.functions import Count, Max, Sum
from tests.contracts.constants import CLICKHOUSE_VERSIONS_WITHOUT_CORRELATED_MEMBERSHIP
from tests.contracts.query_shape_models import (
    ShapeBookmark,
    ShapeItem,
    ShapeReview,
    ShapeScore,
    ShapeShelf,
    ShapeWriter,
)
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def shape_rows():
    async with hare_test_context(
        ["tests.contracts.query_shape_models"], db_url=DatabaseUnderTest.get_url()
    ) as context:
        shelves = [
            ShapeShelf(store=store, number=number, label=label)
            for store, number, label in (("A", 1, "x"), ("A", 2, "y"), ("B", 1, "x"))
        ]
        await ShapeShelf.objects.bulk_create(shelves)
        writers = [ShapeWriter(id=number, name=f"writer{number}") for number in (1, 2, 3)]
        await ShapeWriter.objects.bulk_create(writers)
        await ShapeItem.objects.bulk_create(
            [
                ShapeItem(id=1, score=3, shelf=shelves[0], writer=writers[0]),
                ShapeItem(id=2, score=5, shelf=shelves[1], writer=writers[0]),
                ShapeItem(id=3, score=None, shelf=shelves[1], writer=writers[1]),
                ShapeItem(id=4, score=7, shelf=shelves[2], writer=writers[1]),
                ShapeItem(id=5, score=None),
                ShapeItem(id=6, score=9, shelf=shelves[2], writer=writers[1]),
            ]
        )
        await ShapeReview.objects.bulk_create([ShapeReview(id=number, writer=writers[0]) for number in (1, 2)])
        if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
            await ShapeBookmark.objects.bulk_create([ShapeBookmark(id=1, shelf=shelves[0]), ShapeBookmark(id=2)])
        await writers[0].shelves.add(*shelves[:2])
        await writers[1].shelves.add(shelves[2])
        yield context


@pytest.mark.asyncio
async def test_composite_null_lookups_validate_values_in_every_condition(shape_rows):
    cases = [(ShapeItem, [5], [1, 2, 3, 4, 6])]
    if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
        cases.append((ShapeBookmark, [2], [1]))
    for model, null_ids, present_ids in cases:
        for lookup in ("isnull", "not_isnull"):
            for value in ("false", "", 0, 1, None, 1.0):
                for negated in (False, True):
                    async with capture_queries() as captured:
                        with pytest.raises(UnSupportedError, match="bool"):
                            condition = Q(**{f"shelf__{lookup}": value})
                            await model.objects.filter(~condition if negated else condition)
                    assert captured.count == 0
            for value in (True, False):
                expected = null_ids if value == (lookup == "isnull") else present_ids
                assert (
                    await model.objects.filter(**{f"shelf__{lookup}": value})
                    .order_by("id")
                    .values_list("id", flat=True)
                    == expected
                )


@pytest.mark.asyncio
async def test_subquery_membership_matches_nullable_value_sets(shape_rows):
    for bound, members in ((2, {3, 5}), (3, {3, 5, None}), (0, set())):
        source = ShapeItem.objects.filter(id__lte=bound).values_list("score", flat=True)
        expected = [number for number, score in enumerate((3, 5, None, 7, None, 9), 1) if score in members]
        remaining = [number for number in range(1, 7) if number not in expected]
        for value in (source, Subquery(source)):
            assert (
                await ShapeItem.objects.filter(score__in=value).order_by("id").values_list("id", flat=True) == expected
            )
            for query in (
                ShapeItem.objects.filter(score__not_in=value),
                ShapeItem.objects.exclude(score__in=value),
                ShapeItem.objects.filter(~Q(score__in=value)),
            ):
                assert await query.order_by("id").values_list("id", flat=True) == remaining


@pytest.mark.asyncio
async def test_wrapped_model_subqueries_select_the_same_keys(shape_rows):
    source = ShapeWriter.objects.filter(id__gte=2).order_by()
    for field, expected in (("writer", [3, 4, 6]), ("writer_id", [3, 4, 6]), ("id", [2, 3])):
        for value in (source, Subquery(source)):
            assert (
                await ShapeItem.objects.filter(**{f"{field}__in": value}).order_by("id").values_list("id", flat=True)
                == expected
            )
            remaining = [number for number in range(1, 7) if number not in expected]
            for query in (
                ShapeItem.objects.filter(**{f"{field}__not_in": value}),
                ShapeItem.objects.exclude(**{f"{field}__in": value}),
            ):
                assert await query.order_by("id").values_list("id", flat=True) == remaining
    async with capture_queries() as captured:
        with pytest.raises(QueryError, match="multi-field"):
            await ShapeItem.objects.filter(id__in=Subquery(source.values("id", "name")))
    assert captured.count == 0
    composite_source = ShapeShelf.objects.filter(store="A")
    for value in (composite_source, Subquery(composite_source)):
        assert await ShapeItem.objects.filter(shelf__in=value).order_by("id").values_list("id", flat=True) == [1, 2, 3]


@pytest.mark.asyncio
async def test_nullable_membership_keeps_slices_joins_and_correlations(shape_rows):
    source = ShapeItem.objects.order_by("id").values_list("score", flat=True)
    for selected, members in ((source[2:3], [None]), (source[:2], [3, 5]), (source[6:], [])):
        for negate in (False, True):
            condition = ~Q(items__score__in=selected) if negate else Q(items__score__in=selected)
            listed = ~Q(items__score__in=members) if negate else Q(items__score__in=members)
            # Each result is also pinned to a Python oracle, including the writer without items.
            expected = [2, 3] if members == [None] else [1] if members else []
            if negate:
                expected = [number for number in (1, 2, 3) if number not in expected]
            for predicate in (condition, listed):
                assert (
                    await ShapeWriter.objects.order_by("id").filter(predicate).distinct().values_list("id", flat=True)
                    == expected
                )


@pytest.mark.asyncio
async def test_correlated_membership_compares_null_values(shape_rows):
    connection = ShapeItem._meta.connection
    if not connection.features.supports_correlated_subqueries:
        pytest.skip("The database has no correlated subqueries")
    correlated = (
        ShapeItem.objects.filter(writer_id=OuterReference("writer_id")).order_by().values_list("score", flat=True)
    )
    positive = ShapeItem.objects.filter(score__in=correlated).order_by("id").values_list("id", flat=True)
    negative = ShapeItem.objects.filter(score__not_in=correlated).values_list("id", flat=True)
    if (
        connection.dialect.name == "clickhouse"
        and (await connection.get_server_version())[:2] in CLICKHOUSE_VERSIONS_WITHOUT_CORRELATED_MEMBERSHIP
    ):
        # These versions run correlated scalar subqueries, but explicitly refuse them inside IN.
        for query in (positive, negative):
            with pytest.raises(UnSupportedError, match="Correlated subqueries are not supported as IN"):
                await query
        return
    assert await positive == [1, 2, 3, 4, 6]
    assert await negative == [5]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["shelf", "shelf__pk"])
async def test_composite_relation_aliases_keep_the_key_operations(shape_rows, path):
    query = ShapeItem.objects.values("id", key=path)
    null_key = None if path == "shelf" else (None, None)
    assert await query.filter(key=("A", 2)).order_by("id").values_list("id", flat=True) == [2, 3]
    assert await query.filter(key__in=[("B", 1)]).order_by("id").values_list("id", flat=True) == [4, 6]
    assert await query.filter(key=null_key).values_list("id", flat=True) == [5]
    for order, expected in (("key", [("A", 1), ("A", 2), ("B", 1)]), ("-key", [("B", 1), ("A", 2), ("A", 1)])):
        assert await query.exclude(key=null_key).order_by(order).values_list("key", flat=True).distinct() == expected
    assert await query.filter(id=1).values("key") == [{"key": ("A", 1)}]


@pytest.mark.asyncio
async def test_exists_refuses_an_inflated_aggregate_used_by_having(shape_rows):
    source = ShapeWriter.objects.order_by().annotate(total=Count("items")).filter(reviews__id__gte=1)
    writer = await ShapeWriter.objects.order_by().get(id=1)
    invalid = source.filter(total__gte=3)
    for make_query in (
        lambda: invalid.exists(),
        lambda: invalid.contains(writer),
        lambda: ShapeWriter.objects.order_by().filter(Exists(invalid)),
        lambda: ShapeWriter.objects.order_by().annotate(found=Exists(invalid)).values_list("found", flat=True),
    ):
        async with capture_queries() as captured:
            with pytest.raises(QueryError, match="to-many"):
                await make_query()
        assert captured.count == 0
    assert await source.exists() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ordering, expected",
    [(None, [1, 1, 2, 2, 2, 3]), ("items__id", [1, 1, 2, 2, 2, 3]), ("shelves__number", [1, 1, 2, 3])],
)
async def test_summaries_and_union_preserve_rows_added_by_ordering(shape_rows, ordering, expected):
    source = ShapeWriter.objects.all() if ordering is None else ShapeWriter.objects.order_by(ordering)
    for query, read in (
        (source, attrgetter("id")),
        (source.values("id"), itemgetter("id")),
        (source.values_list("id"), itemgetter(0)),
        (source.values_list("id", flat=True), lambda row: row),
        (source.values_list("id", named=True), itemgetter(0)),
    ):
        assert Counter(map(read, await query)) == Counter(expected)
        assert await query.count() == len(expected)
        for start, end in ((1, 3), (2, None), (5, None)):
            sliced = query[start:end]
            assert await sliced.count() == len(expected[start:end])
            assert await sliced.exists() is bool(expected[start:end])
        combined = query.union(query, all=True)
        assert Counter(map(read, await combined)) == Counter(expected * 2)
        assert await combined.count() == len(expected) * 2


@pytest.mark.asyncio
async def test_union_preserves_distinct_columns_added_by_ordering(shape_rows):
    query = ShapeShelf.objects.order_by("label").values_list("store", flat=True).distinct()
    assert Counter(await query) == Counter({"A": 2, "B": 1})
    combined = query.union(query, all=True)
    assert Counter(await combined) == Counter({"A": 4, "B": 2})
    assert await combined.count() == 6


@pytest.mark.asyncio
async def test_aggregates_read_every_component_of_grouped_keys(shape_rows):
    for path in ("shelf", "shelf__pk"):
        for names in ((path,), (path, "writer")):
            query = ShapeItem.objects.exclude(shelf=None).annotate(total=Count("id")).group_by(*names)
            expected = {"maximum": 2, "count": 3 if len(names) == 1 else 4, "sum": 5}
            assert await query.aggregate(maximum=Max("total"), count=Count("total"), sum=Sum("total")) == expected
    query = ShapeShelf.objects.annotate(total=Count("items")).group_by("pk")
    assert await query.aggregate(maximum=Max("total"), count=Count("total"), sum=Sum("total")) == {
        "maximum": 2,
        "count": 3,
        "sum": 5,
    }


@pytest.mark.asyncio
async def test_implicit_grouping_does_not_add_unselected_ordering_columns(shape_rows):
    for ordering in ("label", "-label"):
        query = ShapeShelf.objects.values("store").annotate(total=Count("items")).order_by(ordering)
        sql = query.sql()
        grouped = sql.split("GROUP BY", 1)[1].split("ORDER BY", 1)[0]
        assert "label" not in grouped
        if DatabaseUnderTest.get_dialect().name == "sqlite":
            assert sorted((row["store"], row["total"]) for row in await query) == [("A", 3), ("B", 2)]
            assert await query.count() == 2
            assert await query[2:].exists() is False
            assert len(await query.union(query, all=True)) == 4
            assert len([row async for row in query.iterator(chunk_size=1)]) == 2
        else:
            with pytest.raises(OperationalError):
                await query


@pytest.mark.asyncio
async def test_bulk_update_returning_is_explicit_or_inherited(shape_rows):
    obj = await ShapeScore.objects.create(id=1, score=1)
    obj.score = 21
    if not DatabaseUnderTest.get_dialect().features.supports_returning:
        async with capture_queries() as captured:
            with pytest.raises(UnSupportedError, match="RETURNING"):
                await ShapeScore.objects.bulk_update([obj], ["score"], returning=True)
        assert captured.count == 0
        assert await ShapeScore.objects.values_list("score", flat=True) == [1]
        await ShapeScore.objects.bulk_update([obj], ["score"])
        with pytest.raises(NoValuesFetched):
            _ = obj.doubled
    else:
        await ShapeScore.objects.bulk_update([obj], ["score"], returning=True)
        assert obj.doubled == 42
    assert await ShapeScore.objects.values_list("doubled", flat=True) == [42]
