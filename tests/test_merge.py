"""Model.objects.merge(source, on=...): one MERGE matching a list of rows or a queryset to the model's
rows - update, delete, insert and do-nothing branches with conditions, the target queryset's filters,
the version bump and the auto_now moment, the active tenant, RETURNING - and what it refuses."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F, Q
from tests.testmodels import (
    DeletePreviewTenantProject,
    IntFields,
    MergeDelivery,
    MergeStock,
    MergeSupplier,
    SoftDeleteAutoNow,
)


def upsert(merge):
    return merge.when_matched(update={"count": F("count") + F("merge_source__count")}).when_not_matched(
        insert={"sku": F("merge_source__sku"), "count": F("merge_source__count")}
    )


async def get_stock() -> dict[str, tuple[int, int]]:
    return {stock.sku: (stock.count, stock.version) for stock in await MergeStock.objects.all()}


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_an_upsert_from_a_list_of_rows(db):
    old = await MergeStock.objects.create(sku="a", count=1)
    async with capture_queries() as queries:
        written = await upsert(
            MergeStock.objects.merge([{"sku": "a", "count": 5}, {"sku": "b", "count": 2}], on="sku")
        )
    assert written == 2
    assert queries.count == 1
    assert queries.queries[0].lstrip().upper().startswith("MERGE INTO")
    assert await get_stock() == {"a": (6, 1), "b": (2, 0)}
    updated = await MergeStock.objects.get(sku="a")
    assert updated.updated_at > old.updated_at
    assert (await MergeStock.objects.get(sku="b")).updated_at is not None
    assert await MergeStock.objects.merge([], on="sku").when_matched(delete=True) == 0


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_values_queryset_source_and_a_relation(db):
    supplier = await MergeSupplier.objects.create(id=1, name="acme")
    await MergeStock.objects.create(sku="a", count=1)
    for sku, delivered in [("a", 3), ("c", 4)]:
        await MergeDelivery.objects.create(sku=sku, delivered=delivered)
    written = await (
        MergeStock.objects.merge(MergeDelivery.objects.values("sku", "delivered"), on={"sku": "sku"})
        .when_matched(update={"count": F("count") + F("merge_source__delivered"), "supplier": supplier})
        .when_not_matched(insert={"sku": F("merge_source__sku"), "count": F("merge_source__delivered") * 10})
    )
    assert written == 2
    assert await get_stock() == {"a": (4, 1), "c": (40, 0)}
    assert (await MergeStock.objects.get(sku="a")).supplier_id == 1
    rows = [{"sku": "d", "count": 1, "supplier": supplier}]
    await MergeStock.objects.merge(rows, on="sku").when_not_matched(
        insert={
            "sku": F("merge_source__sku"),
            "count": F("merge_source__count"),
            "supplier": F("merge_source__supplier"),
        }
    )
    assert (await MergeStock.objects.get(sku="d")).supplier_id == 1


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_model_queryset_a_union_and_differently_named_columns(db):
    await MergeStock.objects.create(sku="a", count=1)
    for sku, delivered in [("a", 2), ("e", 5), ("f", 6)]:
        await MergeDelivery.objects.create(sku=sku, delivered=delivered)
    deliveries = MergeDelivery.objects.filter(sku__in=["a", "e"])
    written = await (
        MergeStock.objects.merge(deliveries, on="sku")
        .when_matched(update={"count": F("merge_source__delivered")})
        .when_not_matched(insert={"sku": F("merge_source__sku"), "count": F("merge_source__delivered")})
    )
    assert written == 2
    union = (
        MergeDelivery.objects.filter(sku="f")
        .values(code="sku", amount="delivered")
        .union(MergeDelivery.objects.filter(sku="a").values(code="sku", amount="delivered"))
    )
    written = await (
        MergeStock.objects.merge(union, on={"sku": "code"})
        .when_matched(update={"count": F("count") + F("merge_source__amount")})
        .when_not_matched(insert={"sku": F("merge_source__code"), "count": F("merge_source__amount")})
    )
    assert written == 2
    assert {sku: count for sku, (count, _version) in (await get_stock()).items()} == {"a": 4, "e": 5, "f": 6}


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_conditions_delete_and_do_nothing_in_order(db):
    for sku, count in [("a", 1), ("b", 10), ("c", 5)]:
        await MergeStock.objects.create(sku=sku, count=count)
    source = [{"sku": "a", "count": 1}, {"sku": "b", "count": 3}, {"sku": "c", "count": 2}, {"sku": "z", "count": 0}]
    written = await (
        MergeStock.objects.merge(source, on="sku")
        .when_matched(delete=True, condition=Q(count__lte=F("merge_source__count")))
        .when_matched(do_nothing=True, condition=Q(sku="c"))
        .when_matched(update={"count": F("count") - F("merge_source__count")})
        .when_not_matched(do_nothing=True)
    )
    assert written == 2
    assert await get_stock() == {"b": (7, 1), "c": (5, 0)}


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_not_matched_condition_over_the_source(db):
    source = [{"sku": "x", "count": 0}, {"sku": "y", "count": 4}, {"sku": "w", "count": 9}]
    written = await (
        MergeStock.objects.merge(source, on="sku")
        .when_not_matched(do_nothing=True, condition=Q(merge_source__count__gte=9))
        .when_not_matched(
            insert={"sku": F("merge_source__sku"), "count": F("merge_source__count") * 2},
            condition=Q(merge_source__sku__in=["x", "y", "w"]) | Q(merge_source__count=0),
        )
    )
    assert written == 2
    assert await get_stock() == {"x": (0, 0), "y": (8, 0)}
    with pytest.raises(FieldError, match="reads a column of merge\\(\\)'s source \\(sku, count\\), got 'other'"):
        await MergeStock.objects.merge(source, on="sku").when_not_matched(insert={"sku": F("merge_source__other")})


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_the_target_queryset_limits_the_matched_rows(db):
    supplier = await MergeSupplier.objects.create(id=1, name="acme")
    await MergeStock.objects.create(sku="a", count=1, supplier=supplier)
    await MergeStock.objects.create(sku="b", count=1)
    source = [{"sku": "a", "count": 5}, {"sku": "b", "count": 5}]
    written = (
        await MergeStock.objects.filter(supplier=supplier)
        .merge(source, on="sku")
        .when_matched(update={"count": F("merge_source__count")})
    )
    assert written == 1
    assert await get_stock() == {"a": (5, 1), "b": (1, 0)}


@requires_features(supports_merge_not_matched_by_source=True)
@pytest.mark.asyncio
async def test_rows_no_source_row_matches(db):
    supplier = await MergeSupplier.objects.create(id=1, name="acme")
    for sku in ("a", "b", "c"):
        await MergeStock.objects.create(sku=sku, count=1, supplier=supplier)
    await MergeStock.objects.create(sku="other", count=1)
    written = await (
        MergeStock.objects.filter(supplier=supplier)
        .merge([{"sku": "a", "count": 2}], on="sku")
        .when_matched(update={"count": F("merge_source__count")})
        .when_not_matched_by_source(delete=True, condition=Q(sku="b"))
        .when_not_matched_by_source(update={"count": 0})
    )
    assert written == 3
    assert await get_stock() == {"a": (2, 1), "c": (0, 1), "other": (1, 0)}


@requires_features(supports_merge_returning=True)
@pytest.mark.asyncio
async def test_returning_the_written_rows(db):
    await MergeStock.objects.create(sku="a", count=1)
    await MergeStock.objects.create(sku="gone", count=1)
    rows = await (
        MergeStock.objects.merge(
            [{"sku": "a", "count": 2}, {"sku": "n", "count": 3}, {"sku": "gone", "count": 0}], on="sku"
        )
        .when_matched(delete=True, condition=Q(sku="gone"))
        .when_matched(update={"count": F("count") + F("merge_source__count")})
        .when_not_matched(insert={"sku": F("merge_source__sku"), "count": F("merge_source__count")})
        .returning("sku", "count", "version")
    )
    assert sorted(rows, key=lambda row: row["sku"]) == [
        {"sku": "a", "count": 3, "version": 1, "merge_action": "update"},
        {"sku": "gone", "count": 1, "version": 0, "merge_action": "delete"},
        {"sku": "n", "count": 3, "version": 0, "merge_action": "insert"},
    ]


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_the_active_tenant(db):
    with Tenancy.scope(3):
        await DeletePreviewTenantProject.objects.create(name="old")
        written = await (
            DeletePreviewTenantProject.objects.merge([{"name": "old"}, {"name": "new"}], on="name")
            .when_matched(do_nothing=True)
            .when_not_matched(insert={"name": F("merge_source__name")})
        )
        assert written == 1
        assert sorted(await DeletePreviewTenantProject.objects.values_list("name", flat=True)) == ["new", "old"]
        with pytest.raises(QueryError, match="from an expression under the active tenant scope"):
            await DeletePreviewTenantProject.objects.merge([{"name": "x"}], on="name").when_not_matched(
                insert={"name": F("merge_source__name"), "company_id": F("merge_source__name")}
            )
        with pytest.raises(QueryError, match="outside the active tenant scope"):
            await DeletePreviewTenantProject.objects.merge([{"name": "old"}], on="name").when_matched(
                update={"company_id": 4}
            )
    with Tenancy.scope(4):
        # Another tenant's "old" isn't matched - inserted as this tenant's.
        await (
            DeletePreviewTenantProject.objects.merge([{"name": "old"}], on="name")
            .when_matched(do_nothing=True)
            .when_not_matched(insert={"name": F("merge_source__name")})
        )
    assert await DeletePreviewTenantProject.objects.all_tenants().filter(name="old").count() == 2


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_server_before_17_refuses_returning_and_by_source(db, monkeypatch):
    connection = MergeStock.get_connection()
    monkeypatch.setattr(
        connection,
        "features",
        connection.features.replace(supports_merge_returning=False, supports_merge_not_matched_by_source=False),
    )
    merge = MergeStock.objects.merge([{"sku": "a"}], on="sku")
    async with capture_queries() as queries:
        with pytest.raises(UnSupportedError, match="returning\\(\\) needs MERGE ... RETURNING"):
            await merge.when_matched(delete=True).returning("sku")
        with pytest.raises(UnSupportedError, match="when_not_matched_by_source\\(\\) needs a MERGE"):
            await merge.when_not_matched_by_source(delete=True)
    assert queries.count == 0


@requires_features(supports_merge=False)
@pytest.mark.asyncio
async def test_a_database_without_merge_refuses_it(db):
    async with capture_queries() as queries:
        with pytest.raises(UnSupportedError, match="merge\\(\\) needs MERGE"):
            await upsert(MergeStock.objects.merge([{"sku": "a", "count": 1}], on="sku"))
    assert queries.count == 0


@requires_features(supports_merge=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (lambda: MergeStock.objects.merge([{"sku": "a"}], on="sku"), QueryError, "needs a branch"),
        (
            lambda: MergeStock.objects.merge([{"sku": "a"}], on="sku").when_matched(update={"id": 1}),
            QueryError,
            "can't update the primary key",
        ),
        (
            lambda: MergeStock.objects.merge([{"sku": "a"}], on="sku").when_matched(update={"missing": 1}),
            FieldError,
            "got 'missing'",
        ),
        (
            lambda: (
                MergeStock.objects.annotate(double=F("count") * 2)
                .merge([{"sku": "a"}], on="sku")
                .when_matched(delete=True)
            ),
            QueryError,
            "only filters the rows",
        ),
        (
            lambda: SoftDeleteAutoNow.objects.merge([{"name": "a"}], on="name").when_matched(delete=True),
            QueryError,
            "in the database alone",
        ),
        (
            lambda: (
                MergeStock.objects.filter(supplier__name="x").merge([{"sku": "a"}], on="sku").when_matched(delete=True)
            ),
            QueryError,
            "can't cross a relation",
        ),
    ],
)
async def test_a_merge_it_cant_run_is_refused(db, make, error, message):
    async with capture_queries() as queries:
        with pytest.raises(error, match=message):
            await make()
    assert queries.count == 0


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: MergeStock.objects.merge(5, on="sku"), "takes a queryset or a list of dicts"),
        (lambda: MergeStock.objects.merge([{"sku": "a"}, {"count": 1}], on="sku"), "rows of the same keys"),
        (
            lambda: MergeStock.objects.merge(IntFields.objects.values_list("id"), on="sku"),
            "give a values\\(\\) queryset",
        ),
        (lambda: MergeStock.objects.merge([], on=[]), "merge\\(on=...\\) takes field names"),
        (lambda: MergeStock.objects.merge([], on="sku").when_matched(), "takes one of"),
        (
            lambda: MergeStock.objects.merge([], on="sku").when_matched(update={"count": 1}, delete=True),
            "takes one of",
        ),
        (lambda: MergeStock.objects.merge([], on="sku").when_matched(delete="yes"), "takes a bool"),
        (lambda: MergeStock.objects.merge([], on="sku").when_not_matched(insert={}), "takes a dict of field values"),
        (lambda: MergeStock.objects.merge([], on="sku").when_matched(delete=True, condition={"a": 1}), "takes a Q"),
        (lambda: MergeStock.objects.merge([], on="sku").returning(), "takes the names of the fields returned"),
    ],
)
def test_a_wrong_call_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()


def test_an_on_field_not_stored_is_refused():
    with pytest.raises(FieldError, match="got 'stocks'"):
        MergeSupplier.objects.merge([], on="stocks")
