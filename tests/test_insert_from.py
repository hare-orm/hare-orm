"""Model.objects.insert_from(queryset, fields=[...]): INSERT ... SELECT of the rows a queryset selects -
a values()/values_list() or a model queryset, a relation by its key, the auto_now moment and the active
tenant filled in - and the fields, sources and tenants it refuses."""

from __future__ import annotations

import datetime

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import FieldError, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F
from tests.testmodels import (
    DeletePreviewTenantProject,
    DeletePreviewTenantTask,
    DocumentRevisionNote,
    Event,
    IntFields,
    Tournament,
    VersionedDocumentWithComputedColumns,
)


async def create_int_rows() -> None:
    for intnum in range(10, 60, 10):
        await IntFields.objects.create(intnum=intnum)


@pytest.mark.asyncio
async def test_the_rows_of_a_values_queryset(db):
    await create_int_rows()
    async with capture_queries() as queries:
        written = await IntFields.objects.insert_from(
            IntFields.objects.filter(intnum__lte=20).values("intnum", plus_one=F("intnum") + 1),
            fields=["intnum", "intnum_null"],
        )
    assert written == 2
    assert queries.count == 1
    assert queries.queries[0].lstrip().upper().startswith("INSERT INTO")
    rows = (
        await IntFields.objects.filter(intnum_null__isnull=False)
        .order_by("intnum")
        .values_list("intnum", "intnum_null")
    )
    assert rows == [(10, 11), (20, 21)]


@pytest.mark.asyncio
async def test_a_model_queryset_an_ordered_slice_and_no_row(db):
    await create_int_rows()
    assert await IntFields.objects.insert_from(IntFields.objects.filter(intnum__gte=40), fields=["intnum"]) == 2
    top = IntFields.objects.order_by("-intnum").values_list("intnum", "intnum")[:1]
    assert await IntFields.objects.insert_from(top, fields=["intnum", "intnum_null"]) == 1
    assert await IntFields.objects.filter(intnum_null=50).count() == 1
    assert (
        await IntFields.objects.insert_from(IntFields.objects.filter(intnum=0).values("intnum"), fields=["intnum"])
        == 0
    )
    assert await IntFields.objects.count() == 8


@pytest.mark.asyncio
async def test_a_union_source(db):
    await create_int_rows()
    union = (
        IntFields.objects.filter(intnum=10)
        .values_list("intnum", "intnum")
        .union(IntFields.objects.filter(intnum=50).values_list("intnum", "intnum"))
    )
    assert await IntFields.objects.insert_from(union, fields=["intnum", "intnum_null"]) == 2
    copied = await IntFields.objects.filter(intnum_null__isnull=False).values_list("intnum_null", flat=True)
    assert sorted(copied) == [10, 50]


@pytest.mark.asyncio
async def test_a_relation_by_its_key_and_the_auto_now_moment(db):
    first = await Tournament.objects.create(id=1, name="First")
    await Tournament.objects.create(id=2, name="Second")
    before = datetime.datetime.now(datetime.UTC)
    written = await Event.objects.insert_from(
        Tournament.objects.order_by("id").values_list("name", "id", "name"), fields=["name", "tournament", "token"]
    )
    assert written == 2
    events = await Event.objects.order_by("name").select_related("tournament")
    assert [(event.name, event.tournament.name, event.token) for event in events] == [
        ("First", "First", "First"),
        ("Second", "Second", "Second"),
    ]
    assert all(event.modified >= before - datetime.timedelta(seconds=1) for event in events)
    assert await first.events.count() == 1


@pytest.mark.asyncio
async def test_the_active_tenant_is_written(db):
    await Tournament.objects.create(id=1, name="alpha")
    await Tournament.objects.create(id=2, name="beta")
    with Tenancy.scope(7):
        assert (
            await DeletePreviewTenantProject.objects.insert_from(Tournament.objects.values("name"), fields=["name"])
            == 2
        )
        assert sorted(await DeletePreviewTenantProject.objects.values_list("name", flat=True)) == ["alpha", "beta"]
        with pytest.raises(QueryError, match="writes 'company_id' from the source rows under the active tenant scope"):
            await DeletePreviewTenantProject.objects.insert_from(
                Tournament.objects.values_list("name", "id"), fields=["name", "company_id"]
            )
        project = await DeletePreviewTenantProject.objects.first()
        with pytest.raises(QueryError, match="writes 'project' from the source rows under the active tenant scope"):
            await DeletePreviewTenantTask.objects.insert_from(
                DeletePreviewTenantProject.objects.values_list("id", "name"), fields=["project", "name"]
            )
    assert await DeletePreviewTenantProject.objects.all_tenants().filter(company_id=7).count() == 2
    with pytest.raises(QueryError, match="no tenant is active"):
        await DeletePreviewTenantProject.objects.insert_from(Tournament.objects.values("name"), fields=["name"])
    # Every tenant's rows, the tenant read from the source.
    written = await DeletePreviewTenantProject.objects.all_tenants().insert_from(
        Tournament.objects.values_list("name", "id"), fields=["name", "company_id"]
    )
    assert written == 2
    tasks = await DeletePreviewTenantTask.objects.all_tenants().insert_from(
        DeletePreviewTenantProject.objects.all_tenants().filter(id=project.id).values_list("id", "company_id", "name"),
        fields=["project", "company_id", "name"],
    )
    assert tasks == 1
    assert await DeletePreviewTenantProject.objects.all_tenants().filter(company_id__in=[1, 2]).count() == 2


@pytest.mark.asyncio
async def test_the_source_selects_another_number_of_columns(db):
    await create_int_rows()
    with pytest.raises(
        QueryError, match="writes 2 fields \\(intnum, intnum_null\\), but the source queryset selects 1"
    ):
        await IntFields.objects.insert_from(IntFields.objects.values("intnum"), fields=["intnum", "intnum_null"])
    assert await IntFields.objects.count() == 5


@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (lambda: IntFields.objects.insert_from([1, 2], fields=["intnum"]), QueryError, "takes a queryset"),
        (lambda: IntFields.objects.insert_from(IntFields.objects.all(), fields=[]), QueryError, "takes the names"),
        (
            lambda: IntFields.objects.insert_from(IntFields.objects.all(), fields="intnum"),
            QueryError,
            "takes the names",
        ),
        (
            lambda: IntFields.objects.insert_from(IntFields.objects.all(), fields=["missing"]),
            FieldError,
            "got 'missing'",
        ),
        (
            lambda: IntFields.objects.insert_from(IntFields.objects.all(), fields=["intnum", "intnum"]),
            FieldError,
            "writes a field twice",
        ),
        (
            lambda: Tournament.objects.insert_from(Tournament.objects.all(), fields=["events"]),
            FieldError,
            "got 'events'",
        ),
        (
            lambda: DocumentRevisionNote.objects.insert_from(DocumentRevisionNote.objects.all(), fields=["document"]),
            FieldError,
            "a relation to a composite key - name its key fields",
        ),
        (
            lambda: VersionedDocumentWithComputedColumns.objects.insert_from(
                VersionedDocumentWithComputedColumns.objects.all(), fields=["total"]
            ),
            FieldError,
            "it is generated",
        ),
        (
            lambda: (
                IntFields.objects.filter(id=1)
                .union(IntFields.objects.filter(id=2))
                .insert_from(IntFields.objects.all(), fields=["intnum"])
            ),
            QueryError,
            "insert_from\\(\\)",
        ),
    ],
)
def test_a_wrong_call_is_refused(make, error, message):
    with pytest.raises(error, match=message):
        make()
