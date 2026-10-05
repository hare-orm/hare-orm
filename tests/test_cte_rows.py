"""CteRows: the rows of a WITH the queryset attached, read by an ``__in`` filter - a recursive CTE, a
queryset as the CTE's body, a composite key - and the arguments it refuses."""

from __future__ import annotations

import pytest

from hare.exceptions import QueryError
from hare.query.expressions import CteRows
from hare.sql import Table
from tests.testmodels import Category, IntFields, RecursiveCompositeNode


@pytest.mark.asyncio
async def test_the_rows_of_a_recursive_cte(db):
    root = await Category.objects.create(id=1, name="root")
    middle = await Category.objects.create(id=2, name="middle", parent_id=root.id)
    await Category.objects.create(id=3, name="leaf", parent_id=middle.id)
    await Category.objects.create(id=4, name="other")
    query_class = Category._meta.connection.query_class
    category, ancestors = Table("category"), Table("ancestors")
    columns = (category.id, category.parent_id)
    base = query_class.from_(category).select(*columns).where(category.id == 3)
    step = query_class.from_(category).join(ancestors).on(category.id == ancestors.parent_id).select(*columns)
    union_all = base * step
    union_all.base_query.wrap_set_operation_queries = False
    rows = (
        await Category.objects.with_cte("ancestors", union_all)
        .filter(id__in=CteRows("ancestors", "id"))
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert rows == ["root", "middle", "leaf"]


@pytest.mark.asyncio
async def test_a_queryset_as_the_cte_body(db):
    for row_id in range(1, 6):
        await IntFields.objects.create(id=row_id, intnum=row_id * 10)
    for low in (20, 40):
        rows = (
            await IntFields.objects.with_cte("large", IntFields.objects.filter(intnum__gte=low).values("id"))
            .filter(id__in=CteRows("large", "id"))
            .exclude(id=5)
            .order_by("id")
            .values_list("id", flat=True)
        )
        assert rows == [row_id for row_id in range(low // 10, 5)]


@pytest.mark.asyncio
async def test_a_composite_primary_key(db):
    root = await RecursiveCompositeNode.objects.create(a=1, b=1, name="root")
    await RecursiveCompositeNode.objects.create(a=1, b=2, name="child", parent=root)
    await RecursiveCompositeNode.objects.create(a=2, b=1, name="child", parent=root)
    picked = RecursiveCompositeNode.objects.filter(name="child").values("a", "b")
    rows = (
        await RecursiveCompositeNode.objects.with_cte("picked", picked)
        .filter(pk__in=CteRows("picked", "a", "b"))
        .order_by("a", "b")
        .values_list("a", "b")
    )
    assert rows == [(1, 2), (2, 1)]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("",), "takes the CTE's name"),
        ((5, "id"), "takes the CTE's name"),
        (("ancestors",), "takes the names of the columns"),
        (("ancestors", ""), "takes the names of the columns"),
        (("ancestors", "id", 1), "takes the names of the columns"),
    ],
)
def test_a_wrong_argument_is_refused(arguments, message):
    with pytest.raises(QueryError, match=message):
        CteRows(*arguments)
