"""Read recursive CTEs and composite keys through CteRows, and check the arguments it refuses."""

from __future__ import annotations

import pytest

from hare.core.hare_context import HareContext
from hare.exceptions import QueryError
from hare.query.expressions import CteRows
from hare.sql import Table
from tests.shared_models.testmodels import Category, RecursiveCompositeNode


@pytest.mark.asyncio
async def test_the_rows_of_a_recursive_cte(db):
    root = await Category.objects.create(id=1, name="root")
    middle = await Category.objects.create(id=2, name="middle", parent_id=root.id)
    await Category.objects.create(id=3, name="leaf", parent_id=middle.id)
    await Category.objects.create(id=4, name="other")
    query_class = HareContext.require_current().apps.get_queryset(Category).get_connection().query_class
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
