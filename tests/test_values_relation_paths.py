"""A relation name or a trailing ``pk`` in a field path, like Django - ``values("tags")`` selects the
related primary key (a row per related row, NULL without one), ``values("department")`` the FK
column itself with no JOIN, and ``x__pk`` the primary key field of ``x`` - in values(),
values_list(), order_by(), group_by(), distinct(<fields>), F() and only()."""

import pytest
import pytest_asyncio

from hare.models.tenancy import Tenancy
from hare.query.expressions import F
from hare.query.functions import Count, Max
from tests.testmodels import (
    CompositePkOwningFK,
    RowPinDepartment,
    RowPinEmployee,
    RowPinLabel,
    SharedTopic,
    TenantActiveAuthor,
    TenantActiveAuthorBook,
    TenantArticle,
    Tournament,
)


@pytest_asyncio.fixture
async def staff(db) -> None:
    for department_id, name in ((1, "eng"), (2, "ops"), (3, "empty")):
        await RowPinDepartment.objects.create(id=department_id, name=name)
    labels = {
        label_id: await RowPinLabel.objects.create(
            id=label_id, slug=f"s{label_id}", group=label_id, rank=1, name=f"label{label_id}"
        )
        for label_id in (1, 2, 3)
    }
    employees = {}
    for employee_id, department_id in ((1, 1), (2, 1), (3, 1), (4, 2), (5, None)):
        employees[employee_id] = await RowPinEmployee.objects.create(
            id=employee_id, name=f"e{employee_id}", salary=100 * employee_id, department_id=department_id
        )
    for label_id, employee_id in ((1, 1), (1, 2), (1, 4), (2, 1), (3, 1), (3, 4)):
        await labels[label_id].employees.add(employees[employee_id])


EMPLOYEE_LABELS = [(1, 1), (1, 2), (1, 3), (2, 1), (3, None), (4, 1), (4, 3), (5, None)]
EMPLOYEE_DEPARTMENTS = [(1, 1), (2, 1), (3, 1), (4, 2), (5, None)]
DEPARTMENT_EMPLOYEES = [(1, 1), (1, 2), (1, 3), (2, 4), (3, None)]


def sort_rows(rows):
    return sorted(rows, key=repr)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["labels", "labels__pk", "labels__id"])
async def test_many_to_many(staff, field_name):
    assert sort_rows(await RowPinEmployee.objects.all().values_list("id", field_name)) == sort_rows(EMPLOYEE_LABELS)
    assert sort_rows(await RowPinEmployee.objects.all().values("id", field_name)) == sort_rows(
        [{"id": employee_id, field_name: label_id} for employee_id, label_id in EMPLOYEE_LABELS]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["employees", "employees__pk"])
async def test_reverse_foreign_key(staff, field_name):
    assert sort_rows(await RowPinDepartment.objects.all().values_list("id", field_name)) == sort_rows(
        DEPARTMENT_EMPLOYEES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["department", "department__pk", "department_id"])
async def test_foreign_key(staff, field_name):
    assert sort_rows(await RowPinEmployee.objects.all().values_list("id", field_name)) == sort_rows(
        EMPLOYEE_DEPARTMENTS
    )


@pytest.mark.asyncio
async def test_foreign_key_reads_its_column_without_join(staff):
    assert "JOIN" not in RowPinEmployee.objects.all().values("id", "department").sql()
    assert "JOIN" in RowPinEmployee.objects.all().values("id", "department__pk").sql()


@pytest.mark.asyncio
async def test_renamed_flat_and_named(staff):
    assert await RowPinEmployee.objects.filter(id=4).order_by("labels").values(label="labels") == [
        {"label": 1},
        {"label": 3},
    ]
    assert await RowPinEmployee.objects.filter(id=4).order_by("labels").values_list("labels", flat=True) == [1, 3]
    rows = await RowPinEmployee.objects.filter(id=2).values_list("department", "labels", named=True)
    assert [(row.department, row.labels) for row in rows] == [(1, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["labels", "labels__pk"])
async def test_grouped_by_relation(staff, field_name):
    rows = await RowPinEmployee.objects.all().values(field_name).annotate(employee_count=Count("id"))
    assert sort_rows(rows) == sort_rows(
        [
            {field_name: 1, "employee_count": 3},
            {field_name: 2, "employee_count": 1},
            {field_name: 3, "employee_count": 2},
            {field_name: None, "employee_count": 2},
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["department", "department__pk"])
async def test_group_by(staff, field_name):
    rows = (
        await RowPinEmployee.objects.all()
        .group_by(field_name)
        .annotate(employee_count=Count("id"))
        .values_list(field_name, "employee_count")
    )
    assert sort_rows(rows) == sort_rows([(1, 3), (2, 1), (None, 1)])


@pytest.mark.asyncio
async def test_distinct_count_and_aggregate(staff):
    assert sorted(
        await RowPinEmployee.objects.all().values_list("labels", flat=True).distinct(), key=lambda value: value or 0
    ) == [None, 1, 2, 3]
    assert await RowPinEmployee.objects.all().values("labels").count() == len(EMPLOYEE_LABELS)
    assert await RowPinEmployee.objects.all().values("labels").distinct().count() == 4
    assert await RowPinEmployee.objects.all().values("labels").aggregate(top=Max("labels"), rows=Count("labels")) == {
        "top": 3,
        "rows": 6,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["labels", "labels__pk"])
async def test_order_by(staff, field_name):
    assert await RowPinEmployee.objects.filter(id=1).order_by(f"-{field_name}").values_list("labels", flat=True) == [
        3,
        2,
        1,
    ]
    employees = await RowPinEmployee.objects.filter(id=1).order_by(field_name)
    assert [employee.id for employee in employees] == [1, 1, 1]


@pytest.mark.asyncio
async def test_order_by_reverse_relation_and_pk(staff):
    assert await RowPinDepartment.objects.filter(id__in=[1, 2]).order_by("-employees__pk").values_list(
        "id", "employees"
    ) == [
        (2, 4),
        (1, 3),
        (1, 2),
        (1, 1),
    ]
    assert await RowPinEmployee.objects.filter(department__isnull=False).order_by("-department__pk", "id").values_list(
        "id", flat=True
    ) == [4, 1, 2, 3]


@pytest.mark.asyncio
async def test_first_and_iterator(staff):
    assert await RowPinEmployee.objects.filter(id=1).values("labels").annotate(employee_count=Count("id")).first() == {
        "labels": 1,
        "employee_count": 1,
    }
    rows = [
        row
        async for row in RowPinEmployee.objects.all().order_by("id").values_list("id", "labels").iterator(chunk_size=2)
    ]
    assert [employee_id for employee_id, _label_id in rows] == [1, 1, 1, 2, 3, 4, 4, 5]
    assert sort_rows(rows) == sort_rows(EMPLOYEE_LABELS)


@pytest.mark.asyncio
async def test_union_and_subquery(staff):
    union = (
        RowPinEmployee.objects.filter(id=1)
        .values_list("labels", flat=True)
        .union(RowPinEmployee.objects.filter(id=4).values_list("labels", flat=True))
    )
    assert sorted(await union) == [1, 2, 3]
    for field_name in ("labels", "labels__pk"):
        labels = await RowPinLabel.objects.filter(
            id__in=RowPinEmployee.objects.filter(id=4).values(field_name)
        ).order_by("id")
        assert [label.id for label in labels] == [1, 3]


@pytest.mark.asyncio
async def test_f_expression(staff):
    rows = (
        await RowPinEmployee.objects.filter(id=4).annotate(label_pk=F("labels__pk")).values_list("label_pk", flat=True)
    )
    assert sorted(rows) == [1, 3]
    rows = (
        await RowPinEmployee.objects.filter(id=4)
        .annotate(department_pk=F("department__pk"))
        .values_list("department_pk", flat=True)
    )
    assert rows == [2]


@pytest.mark.asyncio
async def test_only(staff):
    employee = await RowPinEmployee.objects.filter(id=2).only("pk", "department").get()
    assert (employee.id, employee.department_id) == (2, 1)
    with pytest.raises(AttributeError):
        _ = employee.name


@pytest.mark.asyncio
async def test_distinct_on(staff):
    if not RowPinEmployee._meta.db.dialect.supports_distinct_on:
        pytest.skip("DISTINCT ON is PostgreSQL-only")
    rows = (
        await RowPinEmployee.objects.filter(department__isnull=False)
        .order_by("department__pk", "id")
        .distinct("department__pk")
        .values_list("department__pk", "id")
    )
    assert rows == [(1, 1), (2, 4)]


@pytest.mark.asyncio
async def test_composite_primary_key_relation(db):
    tournament = await Tournament.objects.create(name="t")
    await CompositePkOwningFK.objects.create(a=2, b=1, name="late", tournament=tournament)
    await CompositePkOwningFK.objects.create(a=1, b=2, name="early", tournament=tournament)
    for field_name in ("composite_owners", "composite_owners__pk"):
        assert await Tournament.objects.all().order_by(field_name).values_list(field_name, flat=True) == [
            (1, 2),
            (2, 1),
        ]
        assert await Tournament.objects.all().values(field_name).annotate(owner_count=Count("id")).order_by(
            field_name
        ) == [
            {field_name: (1, 2), "owner_count": 1},
            {field_name: (2, 1), "owner_count": 1},
        ]
    assert await Tournament.objects.all().order_by("composite_owners").values_list(
        "composite_owners__a", "composite_owners__b"
    ) == [(1, 2), (2, 1)]
    assert await Tournament.objects.all().order_by("-composite_owners__pk").values_list(
        "composite_owners__name", flat=True
    ) == [
        "late",
        "early",
    ]


@pytest.mark.asyncio
async def test_many_to_many_respects_target_scope(db):
    shared_topic = await SharedTopic.objects.create(id=1, name="shared")
    await SharedTopic.objects.create(id=2, name="unlinked")
    with Tenancy.scope(1):
        own_article = await TenantArticle.objects.create(id=1, title="own")
        deleted_article = await TenantArticle.objects.create(id=3, title="deleted")
        await shared_topic.articles.add(own_article, deleted_article)
        await deleted_article.delete()
    with Tenancy.scope(2):
        other_tenant_article = await TenantArticle.objects.create(id=2, title="other-tenant")
        await shared_topic.articles.add(other_tenant_article)
    with Tenancy.scope(1):
        for field_name in ("articles", "articles__pk", "articles__id"):
            assert await SharedTopic.objects.all().order_by("id").values_list("name", field_name) == [
                ("shared", 1),
                ("unlinked", None),
            ]
            assert (
                await SharedTopic.objects.all()
                .values(field_name)
                .annotate(topic_count=Count("id"))
                .order_by(field_name)
                .count()
                == 2
            )


@pytest.mark.asyncio
async def test_foreign_key_column_ignores_target_scope(db):
    """Like Django, values("author") reads the FK column - a hidden author's id stays; a path
    through the relation (author__pk) joins it in its default scope."""
    with Tenancy.scope(1):
        hidden_author = await TenantActiveAuthor.objects.create(id=1, name="hidden", company_id=1, is_active=False)
        visible_author = await TenantActiveAuthor.objects.create(id=2, name="visible", company_id=1)
        await TenantActiveAuthorBook.objects.create(id=1, title="hidden-author", author=hidden_author)
        await TenantActiveAuthorBook.objects.create(id=2, title="visible-author", author=visible_author)
        assert await TenantActiveAuthorBook.objects.all().order_by("id").values_list("author", "author__pk") == [
            (1, None),
            (2, 2),
        ]
        assert await TenantActiveAuthor.objects.all().values_list("id", "books") == [(2, 2)]
