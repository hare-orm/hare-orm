"""A filter narrowing a to-many JOIN to at most one related row per row - an equality on the related
primary key or a unique field, or ``__isnull=True`` of a reverse relation - repeats no row, so an
aggregate next to it is exact (matching Django), while every other to-many JOIN is still refused."""

import pytest
import pytest_asyncio

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import OuterRef, Q, Subquery
from hare.query.functions import Count, Sum
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from tests.testmodels import (
    RowPinDepartment,
    RowPinEmployee,
    RowPinLabel,
    RowPinLooseLink,
    RowPinProject,
    RowPinTask,
)

FAN_OUT_MESSAGE = "to-many relation"


@pytest_asyncio.fixture
async def staff(db) -> None:
    for department_id, name in ((1, "eng"), (2, "ops"), (3, "empty")):
        await RowPinDepartment.objects.create(id=department_id, name=name)
    labels = {
        1: await RowPinLabel.objects.create(id=1, slug="s1", code="c1", group=1, rank=1, name="shared"),
        2: await RowPinLabel.objects.create(id=2, slug="s2", code=None, group=1, rank=2, name="shared"),
        3: await RowPinLabel.objects.create(id=3, slug="s3", code=None, group=2, rank=1, name="other"),
    }
    employees = {}
    for employee_id, name, salary, department_id in (
        (1, "boss", 300, 1),
        (2, "a", 100, 1),
        (3, "b", 100, 1),
        (4, "c", 200, 2),
        (5, "d", 50, None),
    ):
        employees[employee_id] = await RowPinEmployee.objects.create(
            id=employee_id, name=name, salary=salary, department_id=department_id
        )
    for label_id, employee_id in ((1, 1), (1, 2), (1, 4), (2, 1), (3, 1), (3, 4)):
        await labels[label_id].employees.add(employees[employee_id])
    for link_id, employee_id in ((1, 1), (2, 1), (3, 2)):
        await RowPinLooseLink.objects.create(id=link_id, employee_id=employee_id, label_id=1)
    for task_id, employee_id, hours in ((1, 1, 3), (2, 1, 4), (3, 2, 5), (4, 4, 1), (5, 4, 2)):
        await RowPinTask.objects.create(id=task_id, employee_id=employee_id, hours=hours)
    for project_id, department_id, cost in ((1, 1, 10), (2, 1, 20), (3, 2, 5)):
        await RowPinProject.objects.create(id=project_id, department_id=department_id, cost=cost)


LABEL_ONE_CONDITIONS = [
    pytest.param(lambda: Q(labels=1), id="relation"),
    pytest.param(lambda: Q(labels__pk=1), id="pk"),
    pytest.param(lambda: Q(labels__id=1), id="pk-field"),
    pytest.param(lambda: Q(labels__slug="s1"), id="unique-field"),
    pytest.param(lambda: Q(labels__code="c1"), id="nullable-unique-field"),
    pytest.param(lambda: Q(labels__group=1, labels__rank=1), id="unique-together"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", LABEL_ONE_CONDITIONS)
async def test_aggregate_over_base_rows(staff, condition):
    assert await RowPinEmployee.objects.filter(condition()).aggregate(total=Sum("salary")) == {"total": 600}
    assert await RowPinEmployee.objects.filter(condition()).values("id").annotate(rows=Count("id")).order_by("id") == [
        {"id": 1, "rows": 1},
        {"id": 2, "rows": 1},
        {"id": 4, "rows": 1},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", LABEL_ONE_CONDITIONS)
async def test_aggregate_over_another_relation(staff, condition):
    rows = await RowPinEmployee.objects.filter(condition()).annotate(task_count=Count("tasks")).order_by("id")
    assert [(employee.id, employee.task_count) for employee in rows] == [(1, 2), (2, 1), (4, 2)]


@pytest.mark.asyncio
async def test_model_instance_value(staff):
    label = await RowPinLabel.objects.get(id=1)
    assert await RowPinEmployee.objects.filter(labels=label).aggregate(total=Sum("salary")) == {"total": 600}


@pytest.mark.asyncio
async def test_grouped_by_values_field(staff):
    assert await RowPinEmployee.objects.filter(labels=1).values("department").annotate(
        task_count=Count("tasks")
    ).order_by("department") == [{"department": 1, "task_count": 3}, {"department": 2, "task_count": 2}]


@pytest.mark.asyncio
async def test_reverse_relation_equality(staff):
    assert await RowPinDepartment.objects.filter(employees=1).annotate(project_count=Count("projects")).values_list(
        "id", "project_count"
    ) == [(1, 2)]
    assert await RowPinDepartment.objects.filter(employees=1).annotate(total=Sum("projects__cost")).values_list(
        "id", "total"
    ) == [(1, 30)]
    assert await RowPinDepartment.objects.filter(employees=1).annotate(salary=Sum("employees__salary")).values_list(
        "id", "salary"
    ) == [(1, 300)]
    # The pinned employee row still repeats once per project - Django would return 600 here.
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await RowPinDepartment.objects.filter(employees=1).annotate(
            total=Sum("projects__cost"), salary=Sum("employees__salary")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["employees__isnull", "employees__id__isnull", "employees__name__isnull"])
async def test_reverse_relation_without_rows(staff, lookup):
    assert await RowPinDepartment.objects.filter(**{lookup: True}).annotate(
        project_count=Count("projects")
    ).values_list("id", "project_count") == [(3, 0)]


@pytest.mark.asyncio
async def test_two_pinned_filter_calls(staff):
    assert await RowPinEmployee.objects.filter(labels=1).filter(labels=3).aggregate(total=Sum("salary")) == {
        "total": 500
    }


@pytest.mark.asyncio
async def test_outer_ref_subquery(staff):
    rows = await RowPinLabel.objects.annotate(
        rows=Subquery(
            RowPinEmployee.objects.filter(labels=OuterRef("pk"))
            .values("id")
            .annotate(count=Count("id"))
            .values("count")[:1]
        )
    ).order_by("id")
    assert [(label.id, label.rows) for label in rows] == [(1, 1), (2, 1), (3, 1)]


@pytest.mark.asyncio
async def test_sliced_aggregate(staff):
    assert await RowPinEmployee.objects.filter(labels=1).order_by("id")[:2].aggregate(total=Sum("salary")) == {
        "total": 400
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__in=[1, 2]), id="in"),
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__name="shared"), id="non-unique-field"),
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__group=1), id="part-of-unique-together"),
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__code=None), id="null-of-nullable-unique"),
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__id__gte=1), id="range"),
        pytest.param(lambda: RowPinEmployee.objects.filter(labels__id__isnull=True), id="many-to-many-isnull"),
        pytest.param(lambda: RowPinEmployee.objects.filter(loose_labels=1), id="through-without-unique-pair"),
        pytest.param(lambda: RowPinEmployee.objects.filter(Q(labels=1) | Q(name="d")), id="or"),
        pytest.param(
            lambda: RowPinEmployee.objects.filter(labels__name="shared").filter(labels=1), id="later-call-pins"
        ),
        pytest.param(
            lambda: RowPinEmployee.objects.filter(labels=1).filter(labels__name="shared"), id="later-call-fans"
        ),
    ],
)
async def test_other_to_many_joins_still_refused(staff, build):
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await build().aggregate(total=Sum("salary"))
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await build().annotate(task_count=Count("tasks"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: RowPinDepartment.objects.filter(employees__isnull=False), id="isnull-false"),
        pytest.param(lambda: RowPinDepartment.objects.filter(employees__salary=100), id="non-unique-field"),
        pytest.param(lambda: RowPinDepartment.objects.filter(employees__labels=1), id="pinned-below-unpinned"),
    ],
)
async def test_reverse_relation_others_still_refused(staff, build):
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await build().annotate(project_count=Count("projects"))


@pytest.mark.asyncio
async def test_pinned_filter_rows_do_not_repeat(staff):
    """A pinned filter repeats no primary key, so keyset iteration and a sliced aggregate apply."""
    employees = [
        employee.id async for employee in RowPinEmployee.objects.filter(labels=1).order_by("id").iterator(chunk_size=1)
    ]
    assert employees == [1, 2, 4]
    assert not ModelRowsQuery(RowPinEmployee.objects.filter(labels=1))._rows_repeat_per_primary_key()
    assert ModelRowsQuery(RowPinEmployee.objects.filter(labels__name="shared"))._rows_repeat_per_primary_key()


@pytest.mark.asyncio
async def test_through_without_unique_pair_repeats_rows(staff):
    """The refused loose link really repeats employee 1 - two link rows to label 1."""
    assert await RowPinEmployee.objects.filter(loose_labels=1).order_by("id").values_list("id", flat=True) == [1, 1, 2]
