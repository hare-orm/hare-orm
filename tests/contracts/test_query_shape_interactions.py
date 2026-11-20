"""Filters, summaries and set operations preserve selected identities and row multiplicities."""

from collections import Counter
from operator import attrgetter, itemgetter

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries, hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import NoValuesFetched, OperationalError, QueryError, UnSupportedError
from hare.query.expressions import CteRows, Exists, F, OuterReference, Q, Subquery
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
from tests.utils.statement_plan_checks import StatementPlanChecks


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
@pytest.mark.parametrize(
    ("model", "field_names", "keys"),
    [
        (ShapeItem, ("id",), [(number,) for number in range(1, 7)]),
        (ShapeShelf, ("store", "number"), [("A", 1), ("A", 2), ("B", 1)]),
    ],
)
async def test_sliced_row_shapes_and_summaries_match_python_keys(shape_rows, model, field_names, keys):
    for members in (keys, keys[::2]):
        values = [key[0] if len(field_names) == 1 else key for key in members]
        for descending in (False, True):
            ordered_keys = sorted(members, reverse=descending)
            orderings = [f"-{name}" if descending else name for name in field_names]
            source = model.objects.filter(pk__in=values).order_by(*orderings)
            for start, stop in ((0, None), (0, 0), (1, 3), (8, None)):
                expected = ordered_keys[start:stop]
                sliced = source[start:stop]
                shapes = [
                    (sliced, lambda obj: tuple(getattr(obj, name) for name in field_names)),
                    (sliced.only(*field_names), lambda obj: tuple(getattr(obj, name) for name in field_names)),
                    (sliced.values(*field_names), lambda row: tuple(row[name] for name in field_names)),
                    (sliced.values_list(*field_names), tuple),
                    (sliced.values_list(*field_names, named=True), tuple),
                ]
                if len(field_names) == 1:
                    shapes.append((sliced.values_list(field_names[0], flat=True), lambda value: (value,)))
                for query, identity in shapes:
                    assert [identity(row) for row in await query] == expected
                    assert await query.count() == len(expected)
                    assert await query.exists() == bool(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field_names", "size"),
    [(ShapeItem, ("id",), 6), (ShapeShelf, ("store", "number"), 3)],
    ids=["single-key", "composite-key"],
)
async def test_scalar_count_subqueries_preserve_selected_rows_and_slices(shape_rows, model, field_names, size):
    for shape in ("models", "values", "distinct-values", "union"):
        source = model.objects.all()
        if shape != "models":
            source = source.values(*field_names)
        if shape == "distinct-values":
            source = source.distinct()
        if shape == "union":
            source = source.union(source)
        source = source.order_by(*field_names)
        for offset, limit in ((0, None), (0, 2), (1, None), (1, 2), (0, 0), (size + 1, None)):
            sliced = source.offset(offset)
            if limit is not None:
                sliced = sliced.limit(limit)
            expected = len(list(range(size))[offset : None if limit is None else offset + limit])
            for _ in range(2):
                result = (
                    await model.objects.order_by(*field_names)
                    .limit(1)
                    .annotate(row_count=Subquery(sliced.count()))
                    .values_list("row_count", flat=True)
                )
                assert result == [expected]
                assert await sliced.count() == expected
        assert await model.objects.limit(1).annotate(row_count=Subquery(source.none().count())).values_list(
            "row_count", flat=True
        ) == [0]
    empty_groups = model.objects.annotate(group_size=Count("pk")).filter(group_size__gt=size)
    assert await model.objects.limit(1).annotate(row_count=Subquery(empty_groups.count())).values_list(
        "row_count", flat=True
    ) == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "first_field", "second_field", "ordered_values"),
    [
        (ShapeItem, "id", "score", [(1, 3), (2, 5), (3, None), (4, 7), (5, None), (6, 9)]),
        (ShapeShelf, "number", "number", [(1, 1), (2, 2), (1, 1)]),
    ],
    ids=["single-key", "composite-key"],
)
async def test_derived_aggregate_columns_preserve_case_distinct_aliases(
    shape_rows, model, first_field, second_field, ordered_values
):
    ordering = HareContext.require_current().apps.get_binding(model).primary_key_attribute_names
    source = model.objects.order_by(*ordering).values(*ordering, X=F(first_field) * 10, x=second_field)
    for shape in ("plain", "distinct", "slice", "union"):
        query = source
        expected_values = ordered_values
        if shape == "distinct":
            query = query.distinct()
        elif shape == "slice":
            query = query.limit(2)
            expected_values = expected_values[:2]
        elif shape == "union":
            query = query.union(query)
        expected = {
            "first_total": sum(first * 10 for first, _ in expected_values),
            "second_total": sum(second for _, second in expected_values if second is not None),
        }
        assert await query.aggregate(first_total=Sum("X"), second_total=Sum("x")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "annotation", "field_name", "expected", "threshold", "filtered_total"),
    [(ShapeItem, F("id") * 2, "id", 42, 6, 15), (ShapeShelf, Count("items"), "number", 5, 1, 3)],
    ids=["scalar-single-key", "aggregate-composite-key"],
)
async def test_aggregate_output_names_preserve_source_annotations(
    shape_rows, model, annotation, field_name, expected, threshold, filtered_total
):
    for declaration in ("annotate", "alias"):
        source = getattr(model.objects, declaration)(total=annotation)
        for selects_values in (False, True):
            query = source.values(field_name, "total") if selects_values else source
            filtered = source.filter(total__gt=threshold)
            if selects_values:
                filtered = filtered.values(field_name, "total")
            for _ in range(2):
                assert await query.aggregate(total=Sum("total")) == {"total": expected}
                assert await filtered.aggregate(total=Sum(field_name)) == {"total": filtered_total}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "relation_name", "size"),
    [(ShapeItem, "writer", 6), (ShapeItem, "shelf", 6), (ShapeShelf, "items", 3)],
    ids=["forward-single-key", "forward-composite-key", "reverse-composite-key"],
)
async def test_aggregate_output_names_can_match_key_and_relation_names(shape_rows, model, relation_name, size):
    assert await model.objects.aggregate(pk=Count("pk")) == {"pk": size}
    assert await model.objects.aggregate(**{relation_name: Count(relation_name)}) == {relation_name: 5}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [(ShapeItem, "id", [1, 2, 3, 4, 5, 6]), (ShapeShelf, "number", [1, 2, 1])],
    ids=["single-key", "composite-key"],
)
async def test_aggregate_derived_columns_keep_grouping_and_ordering_aliases(shape_rows, model, field_name, values):
    expected = sorted({value + 1 for value in values})
    for declaration in ("annotate", "alias"):
        grouped = getattr(model.objects, declaration)(shifted=F(field_name) + 1).group_by("shifted")
        assert await grouped.aggregate(result=Sum("shifted")) == {"result": sum(expected)}
        selected = grouped.order_by("shifted").values("shifted")
        assert await selected.aggregate(result=Sum("shifted")) == {"result": sum(expected)}
        assert await selected[:2].aggregate(result=Sum("shifted")) == {"result": sum(expected[:2])}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [(ShapeItem, "id", [1, 2, 3, 4, 5, 6]), (ShapeShelf, "number", [1, 2, 1])],
    ids=["single-key", "composite-key"],
)
async def test_ordering_preserves_case_distinct_annotation_names(shape_rows, model, field_name, values):
    for shift in (10, 20):
        base = model.objects.annotate(X=F(field_name) * -1, x=F(field_name) + shift)
        for ordering in ("x", "-x", "X", "-X"):
            name = ordering.removeprefix("-")
            selected = [value + shift if name == "x" else -value for value in values]
            expected = sorted(selected, reverse=ordering.startswith("-"))
            source = base.order_by(ordering, "pk")
            for query, read_value in (
                (source, attrgetter(name)),
                (
                    source.only(*HareContext.require_current().apps.get_binding(model).primary_key_attribute_names),
                    attrgetter(name),
                ),
                (source.values("X", "x"), itemgetter(name)),
                (source.values_list("X", "x"), itemgetter(1 if name == "x" else 0)),
                (source.values_list(name, flat=True), lambda value: value),
            ):
                for _ in range(2):
                    assert [read_value(row) for row in await query] == expected
            assert [getattr(obj, name) async for obj in source.iterator(chunk_size=1)] == expected
            assert getattr(await source.first(), name) == expected[0]
            assert getattr(await source.last(), name) == expected[-1]
            distinct = base.values("X", "x").distinct().order_by(ordering)
            assert [row[name] for row in await distinct] == sorted(set(expected), reverse=ordering.startswith("-"))
            combined = base.values("X", "x").union(base.none().values("X", "x")).order_by(ordering)
            assert [row[name] for row in await combined] == sorted(set(expected), reverse=ordering.startswith("-"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [(ShapeItem, "id", [1, 2, 3, 4, 5, 6]), (ShapeShelf, "number", [1, 2, 1])],
    ids=["single-key", "composite-key"],
)
async def test_combined_models_preserve_case_distinct_annotation_values(shape_rows, model, field_name, values):
    for shift in (10, 20):
        base = model.objects.order_by().annotate(X=F(field_name) * -1, x=F(field_name) + shift)
        combined_queries = (
            base.union(base.none()),
            base.union(base.none(), all=True),
            base.intersection(base),
            base.difference(base.none()),
            base.union(base.none()).intersection(base),
        )
        for combined in combined_queries:
            for descending in (False, True):
                source = combined.order_by("-x" if descending else "x", "pk")
                expected = sorted([value + shift for value in values], reverse=descending)
                for _ in range(2):
                    objs = await source
                    assert [obj.x for obj in objs] == expected
                    assert all(obj.X == -(obj.x - shift) for obj in objs)
                assert [obj.x async for obj in source.iterator(chunk_size=1)] == expected


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
                nested = Q(id=3) | (Q(id__lte=2) & predicate)
                assert (
                    await ShapeWriter.objects.order_by("id").filter(predicate).distinct().values_list("id", flat=True)
                    == expected
                )
                assert await ShapeWriter.objects.order_by("id").filter(nested).distinct().values_list(
                    "id", flat=True
                ) == sorted(set(expected) | {3})


@pytest.mark.asyncio
async def test_nested_membership_reuses_its_plan_with_new_nullable_values(shape_rows):
    def select(members):
        return (
            ShapeWriter.objects.filter(Q(id=3) | (Q(id__lte=2) & Q(items__score__in=members)))
            .order_by("id")
            .distinct()
            .values_list("id", flat=True)
        )

    def from_query(bound):
        return select(ShapeItem.objects.filter(id__lt=bound).order_by().values_list("score", flat=True))

    expected = ([1, 3], [1, 2, 3])
    assert await select([3, 5]) == expected[0]
    assert await select([3, 5, None]) == expected[1]
    await StatementPlanChecks.assert_a_later_query_runs_on_its_plan(from_query, (3, 4), expected_results=expected)
    rows, hits = await StatementPlanChecks.count_plan_hits(from_query(1))
    assert (rows, hits) == ([3], 1)


@pytest.mark.asyncio
async def test_correlated_membership_compares_null_values(shape_rows):
    connection = HareContext.require_current().apps.get_queryset(ShapeItem).get_connection()
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
async def test_a_correlated_subquery_accepts_a_renamed_values_column(shape_rows):
    connection = HareContext.require_current().apps.get_queryset(ShapeItem).get_connection()
    if not (
        connection.features.supports_correlated_subqueries
        and connection.features.supports_ordered_correlated_subqueries
    ):
        pytest.skip("The database has no ordered correlated subqueries")
    for offset, minimum, expected in ((0, 3, [5, 9, None]), (1, 3, [3, 7, None]), (0, 8, [None, 9, None])):
        inner = (
            ShapeItem.objects.filter(Q(writer_id=OuterReference("id")) & Q(score__gte=minimum))
            .order_by("-score", "id")
            .offset(offset)
            .limit(1)
        )
        for projection in (inner.values("score"), inner.values(display="score")):
            rows = ShapeWriter.objects.annotate(picked=Subquery(projection)).order_by("id")
            assert await rows.values_list("picked", flat=True) == expected


@pytest.mark.asyncio
async def test_a_distinct_slice_aggregates_composite_keys_selected_through_a_relation(shape_rows):
    selected = ShapeShelf.objects.filter(items__score__gte=3).distinct()
    metrics = {"rows": Count("number"), "total": Sum("number")}
    assert await selected.order_by("store", "number").limit(2).offset(1).aggregate(**metrics) == {
        "rows": 2,
        "total": 3,
    }
    with pytest.raises(QueryError, match="can repeat a primary key"):
        await selected.order_by("items__id").limit(2).aggregate(**metrics)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["shelf", "shelf__pk"])
@pytest.mark.parametrize("declaration", ("values", "alias", "annotate"))
async def test_composite_relation_aliases_keep_the_key_operations(shape_rows, path, declaration):
    query = (
        ShapeItem.objects.values("id", key=path)
        if declaration == "values"
        else getattr(ShapeItem.objects, declaration)(key=F(path))
    )
    null_key = None if path == "shelf" else (None, None)
    keys = [("A", 1), ("A", 2), ("A", 2), ("B", 1), None, ("B", 1)]
    assert await query.order_by("id").values_list("key", flat=True) == keys
    assert await query.order_by("id").values_list("id", "key") == list(enumerate(keys, 1))
    assert [row.key for row in await query.order_by("id").values_list("key", named=True)] == keys
    assert await query.order_by("id").values("key") == [{"key": key} for key in keys]
    assert await query.values_list("key", flat=True).count() == 6
    assert await query.aggregate(total=Count("key"), unique=Count("key", distinct=True)) == {"total": 5, "unique": 3}
    assert await query.filter(key=("A", 2)).order_by("id").values_list("id", flat=True) == [2, 3]
    assert await query.filter(key__in=[("B", 1)]).order_by("id").values_list("id", flat=True) == [4, 6]
    assert await query.filter(key=null_key).values_list("id", flat=True) == [5]
    for order, expected in (("key", [("A", 1), ("A", 2), ("B", 1)]), ("-key", [("B", 1), ("A", 2), ("A", 1)])):
        assert await query.exclude(key=null_key).order_by(order).values_list("key", flat=True).distinct() == expected
    assert await query.filter(id=1).values("key") == [{"key": ("A", 1)}]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ("dict", "renamed", "flat", "tuple", "named"))
async def test_composite_projection_keeps_its_identity_in_summaries_and_set_ordering(shape_rows, shape):
    key_name = "key" if shape == "renamed" else "pk"

    def project(source):
        if shape == "renamed":
            return source.values(key="pk")
        if shape == "dict":
            return source.values("pk")
        return source.values_list("pk", flat=shape == "flat", named=shape == "named")

    def key(row):
        if shape in ("dict", "renamed"):
            return row[key_name]
        return row if shape == "flat" else row[0]

    def count(query):
        return query.aggregate(total=Count(key_name), unique=Count(key_name, distinct=True))

    source = ShapeShelf.objects.order_by()
    selected = project(source)
    if shape in ("dict", "renamed"):
        rows = await source.order_by("store", "number").values(label="label", **{key_name: "pk"})
        assert [tuple(row.items()) for row in rows] == [
            (("label", "x"), (key_name, ("A", 1))),
            (("label", "y"), (key_name, ("A", 2))),
            (("label", "x"), (key_name, ("B", 1))),
        ]
    elif shape == "named":
        rows = await source.order_by("store", "number").values_list("label", "pk", named=True)
        assert [row._fields for row in rows] == [("label", "pk")] * 3
        assert rows == [("x", ("A", 1)), ("y", ("A", 2)), ("x", ("B", 1))]
    assert await count(selected) == {"total": 3, "unique": 3}
    assert await count(selected.distinct()) == {"total": 3, "unique": 3}
    assert await count(selected.order_by("pk")[:2]) == {"total": 2, "unique": 2}
    joined = project(source.filter(items__id__gt=0))
    assert await joined.aggregate(unique=Count(key_name, distinct=True)) == {"unique": 3}
    joined = joined.order_by("store", "number", "items__id")
    assert await count(joined[:5]) == {"total": 5, "unique": 3}
    assert await count(joined[:3]) == {"total": 3, "unique": 2}
    if shape == "renamed":
        grouped = (
            source.filter(items__id__gt=0)
            .values("items__id", key="pk")
            .annotate(per_item=Count("items__id"))
            .group_by("key", "items__id")
        )
        assert Counter(row["key"] for row in await grouped) == Counter({("A", 1): 1, ("A", 2): 2, ("B", 1): 2})
        assert await count(grouped) == {"total": 5, "unique": 3}
    left = project(source.filter(store="A"))
    right = project(source.exclude(number=1, store="A"))
    combined = left.union(right)
    assert list(map(key, await combined.order_by(f"-{key_name}"))) == [("B", 1), ("A", 2), ("A", 1)]
    for operation, total, unique in (
        (combined, 3, 3),
        (left.union(right, all=True), 4, 3),
        (left.intersection(right), 1, 1),
        (left.difference(right), 1, 1),
    ):
        assert await count(operation) == {"total": total, "unique": unique}


@pytest.mark.asyncio
async def test_ordered_model_union_can_project_the_scalar_pk_alias(shape_rows):
    low = ShapeWriter.objects.filter(pk__lte=2).order_by("pk")
    high = ShapeWriter.objects.filter(pk__gte=2).order_by("-pk")
    union = low.union(high).order_by("-pk")
    assert await union.values_list("pk", flat=True) == [3, 2, 1]
    assert await union.values(pk_copy="pk") == [{"pk_copy": 3}, {"pk_copy": 2}, {"pk_copy": 1}]
    reverse = high.union(low).order_by("-pk")
    assert await reverse.values("pk") == [{"pk": 3}, {"pk": 2}, {"pk": 1}]
    named = await reverse.values_list("pk", named=True)
    assert [row._fields for row in named] == [("pk",)] * 3
    assert named == [(3,), (2,), (1,)]


@pytest.mark.asyncio
async def test_explicit_cte_reads_its_sliced_body(shape_rows):
    source = ShapeItem.objects.order_by("id")
    for minimum, limit, inner_keys, outer_keys in (
        (3, 3, [1, 2, 4], [1, 4]),
        (7, 2, [4, 6], [4, 6]),
        (3, 0, [], []),
    ):
        body = source.filter(score__gte=minimum).values("id")[:limit]
        assert await body == [{"id": key} for key in inner_keys]
        outer = source.with_cte("picked", body).filter(id__in=CteRows("picked", "id")).exclude(id=2)
        assert await outer.values_list("id", flat=True) == outer_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 2])
async def test_cursor_pages_use_every_component_to_break_ties(shape_rows, page_size):
    ordered = ShapeShelf.objects.order_by("label", "store", "number")
    expected = [("x", "A", 1), ("x", "B", 1), ("y", "A", 2)]
    positions = ("label", "store", "number")
    assert await ordered.values_list(*positions) == expected

    forward = []
    page = ordered
    for start in range(0, len(expected), page_size):
        values = await page.values_list(*positions)[:page_size]
        assert values == expected[start : start + page_size]
        forward.extend(values)
        page = ordered.after_cursor(*values[-1])
    assert forward == expected
    assert await page.values_list(*positions)[:page_size] == []

    backward = []
    page = ordered.before_cursor("z", "Z", 999)
    for stop in range(len(expected), 0, -page_size):
        values = await page.values_list(*positions).limit(page_size)
        assert values == expected[max(0, stop - page_size) : stop]
        backward[:0] = values
        page = ordered.before_cursor(*values[0])
    assert backward == expected
    assert await page.values_list(*positions) == []


@pytest.mark.asyncio
async def test_output_alias_cannot_replace_another_outputs_relation_path(shape_rows):
    source = ShapeItem.objects.order_by("id")
    pairs = (("selected", "writer__name"), ("writer__name", "shelf__label"))
    expected = [
        {"selected": writer, "writer__name": label}
        for writer, label in (
            ("writer1", "x"),
            ("writer1", "y"),
            ("writer2", "y"),
            ("writer2", "x"),
            (None, None),
            ("writer2", "x"),
        )
    ]
    for renames in (pairs, tuple(reversed(pairs))):
        assert await source.values(**dict(renames)) == expected


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
