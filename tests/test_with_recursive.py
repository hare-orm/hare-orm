"""QuerySet.with_recursive(): the rows reachable through a relation of the model to itself - forward,
reverse and many-to-many, with a depth limit, through cycles, soft-deleted rows and composite keys - and
the relations and limits it refuses."""

from __future__ import annotations

import datetime

import pytest

from hare.exceptions import FieldError, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions.constants import MAX_RECURSIVE_DEPTH
from tests.testmodels import Employee, RecursiveCompositeNode, RecursiveSoftNode, RecursiveTenantNode


async def create_company() -> dict[str, Employee]:
    """CEO -> (A, B); A -> (A1, A2); A1 -> A11."""
    employees: dict[str, Employee] = {}
    for name, manager_name in [("CEO", None), ("A", "CEO"), ("B", "CEO"), ("A1", "A"), ("A2", "A"), ("A11", "A1")]:
        manager = employees[manager_name] if manager_name else None
        employees[name] = await Employee.objects.create(name=name, manager=manager)
    return employees


async def get_names(queryset) -> list[str]:
    return sorted(await queryset.values_list("name", flat=True))


@pytest.mark.asyncio
async def test_a_reverse_relation_reaches_every_descendant(db):
    await create_company()
    assert await get_names(Employee.objects.filter(name="A").with_recursive("team_members")) == [
        "A",
        "A1",
        "A11",
        "A2",
    ]
    assert await get_names(Employee.objects.filter(name="CEO").with_recursive("team_members")) == [
        "A",
        "A1",
        "A11",
        "A2",
        "B",
        "CEO",
    ]


@pytest.mark.asyncio
async def test_a_forward_relation_reaches_every_ancestor(db):
    await create_company()
    assert await get_names(Employee.objects.filter(name="A11").with_recursive("manager")) == ["A", "A1", "A11", "CEO"]
    assert await get_names(Employee.objects.filter(name="CEO").with_recursive("manager")) == ["CEO"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_depth", "expected"),
    [
        (0, ["CEO"]),
        (1, ["A", "B", "CEO"]),
        (2, ["A", "A1", "A2", "B", "CEO"]),
        (3, ["A", "A1", "A11", "A2", "B", "CEO"]),
        (MAX_RECURSIVE_DEPTH, ["A", "A1", "A11", "A2", "B", "CEO"]),
    ],
)
async def test_max_depth_limits_the_steps(db, max_depth, expected):
    await create_company()
    queryset = Employee.objects.filter(name="CEO").with_recursive("team_members", max_depth=max_depth)
    assert await get_names(queryset) == expected


@pytest.mark.asyncio
async def test_several_start_rows_and_no_start_row(db):
    await create_company()
    assert await get_names(Employee.objects.filter(name__in=["A1", "B"]).with_recursive("team_members")) == [
        "A1",
        "A11",
        "B",
    ]
    assert await get_names(Employee.objects.filter(name="nobody").with_recursive("team_members")) == []


@pytest.mark.asyncio
async def test_the_result_reads_like_any_queryset(db):
    employees = await create_company()
    descendants = Employee.objects.filter(name="A").with_recursive("team_members")
    assert await descendants.count() == 4
    assert await descendants.exclude(name="A").order_by("-name").values_list("name", flat=True) == ["A2", "A11", "A1"]
    assert [employee.name for employee in await descendants.filter(manager=employees["A1"])] == ["A11"]
    # As a filter value of another query - the rows outside A's branch.
    outside = Employee.objects.exclude(pk__in=descendants).order_by("name").values_list("name", flat=True)
    assert await outside == ["B", "CEO"]


@pytest.mark.asyncio
async def test_a_cycle_ends(db):
    first = await Employee.objects.create(name="first")
    second = await Employee.objects.create(name="second", manager=first)
    first.manager = second
    await first.save()
    assert await get_names(Employee.objects.filter(name="first").with_recursive("manager")) == ["first", "second"]
    assert await get_names(Employee.objects.filter(name="first").with_recursive("team_members")) == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_a_many_to_many_relation_through_a_cycle(db):
    x = await Employee.objects.create(name="x")
    y = await Employee.objects.create(name="y")
    z = await Employee.objects.create(name="z")
    await Employee.objects.create(name="alone")
    await x.talks_to.add(y)
    await y.talks_to.add(z)
    await z.talks_to.add(x)
    assert await get_names(Employee.objects.filter(name="y").with_recursive("talks_to")) == ["x", "y", "z"]
    assert await get_names(Employee.objects.filter(name="y").with_recursive("talks_to", max_depth=1)) == ["y", "z"]
    assert await get_names(Employee.objects.filter(name="y").with_recursive("gets_talked_to", max_depth=1)) == [
        "x",
        "y",
    ]


@pytest.mark.asyncio
async def test_a_soft_deleted_row_is_not_walked_through(db):
    root = await RecursiveSoftNode.objects.create(id=1, name="root")
    middle = await RecursiveSoftNode.objects.create(id=2, name="middle", parent=root)
    await RecursiveSoftNode.objects.create(id=4, name="other", parent=root)
    await middle.delete()
    # A live child under the deleted row.
    await RecursiveSoftNode.objects.create(id=3, name="leaf", parent=middle)
    assert isinstance(middle.deleted_at, datetime.datetime)
    assert await get_names(RecursiveSoftNode.objects.filter(id=1).with_recursive("children")) == ["other", "root"]
    # With the deleted rows the walk reaches the leaf below them.
    assert await get_names(RecursiveSoftNode.objects.include_deleted().filter(id=1).with_recursive("children")) == [
        "leaf",
        "middle",
        "other",
        "root",
    ]
    assert await get_names(RecursiveSoftNode.objects.include_deleted().filter(id=3).with_recursive("parent")) == [
        "leaf",
        "middle",
        "root",
    ]
    # A live start row's walk up stops at the deleted parent.
    assert await get_names(RecursiveSoftNode.objects.filter(id=3).with_recursive("parent")) == ["leaf"]


@pytest.mark.asyncio
async def test_the_active_tenants_rows_only(db):
    for company_id, first_id in [(1, 1), (2, 10)]:
        with Tenancy.scope(company_id):
            root = await RecursiveTenantNode.objects.create(
                id=first_id, company_id=company_id, name=f"root {company_id}"
            )
            await RecursiveTenantNode.objects.create(
                id=first_id + 1, company_id=company_id, name=f"child {company_id}", parent=root
            )
    walk = RecursiveTenantNode.objects.filter(name__startswith="root").with_recursive("children")
    with Tenancy.scope(1):
        assert await get_names(walk) == ["child 1", "root 1"]
    with Tenancy.scope(2):
        assert await get_names(walk) == ["child 2", "root 2"]
    every_tenant = RecursiveTenantNode.objects.all_tenants().filter(name__startswith="root").with_recursive("children")
    assert await get_names(every_tenant) == ["child 1", "child 2", "root 1", "root 2"]


@pytest.mark.asyncio
async def test_an_ordered_sliced_start(db):
    await create_company()
    # The first two by name: A and A1.
    start = Employee.objects.order_by("name")[:2]
    assert await get_names(start.with_recursive("team_members")) == ["A", "A1", "A11", "A2"]


@pytest.mark.asyncio
async def test_a_composite_primary_key(db):
    root = await RecursiveCompositeNode.objects.create(a=1, b=1, name="root")
    child = await RecursiveCompositeNode.objects.create(a=1, b=2, name="child", parent=root)
    await RecursiveCompositeNode.objects.create(a=2, b=1, name="grandchild", parent=child)
    await RecursiveCompositeNode.objects.create(a=2, b=2, name="other")
    assert await get_names(RecursiveCompositeNode.objects.filter(a=1, b=1).with_recursive("children")) == [
        "child",
        "grandchild",
        "root",
    ]
    assert await get_names(RecursiveCompositeNode.objects.filter(name="grandchild").with_recursive("parent")) == [
        "child",
        "grandchild",
        "root",
    ]
    assert await get_names(
        RecursiveCompositeNode.objects.filter(name="root").with_recursive("children", max_depth=1)
    ) == ["child", "root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["name", "talks_to__name", "missing"])
async def test_a_relation_not_to_the_model_itself_is_refused(db, relation):
    with pytest.raises(FieldError, match="isn't a relation of Employee to itself"):
        await Employee.objects.all().with_recursive(relation)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Employee.objects.with_recursive(""), "takes a relation name"),
        (lambda: Employee.objects.with_recursive(5), "takes a relation name"),
        (lambda: Employee.objects.with_recursive("manager", max_depth=-1), "takes None or an int"),
        (
            lambda: Employee.objects.with_recursive("manager", max_depth=MAX_RECURSIVE_DEPTH + 1),
            "takes None or an int",
        ),
        (lambda: Employee.objects.with_recursive("manager", max_depth=True), "takes None or an int"),
        (lambda: Employee.objects.with_recursive("manager", max_depth=2.0), "takes None or an int"),
        (
            lambda: (
                Employee.objects.filter(name="a").union(Employee.objects.filter(name="b")).with_recursive("manager")
            ),
            "with_recursive",
        ),
    ],
)
def test_a_wrong_call_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()
