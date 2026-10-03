import pytest
import pytest_asyncio

from hare.exceptions import (
    DoesNotExist,
    QueryError,
)
from hare.models.tenancy import Tenancy
from tests.testmodels import (
    Author,
    SoftDeleteChildCascadeHard,
    SoftDeleteChildCascadeSoft,
    SoftDeleteParent,
    SoftDeleteStandalone,
    TenantScopedFactory,
    TenantScopedWidget,
)


@pytest_asyncio.fixture
async def parents(db):
    """Live "A"/"C", soft-deleted "B"/"D"."""
    created = {name: await SoftDeleteParent.objects.create(name=name) for name in ("A", "B", "C", "D")}
    await created["B"].delete()
    await created["D"].delete()
    return created


async def names_of(queryset) -> list[str]:
    return [row.name for row in await queryset.order_by("name")]


@pytest.mark.asyncio
async def test_only_deleted_on_model_and_queryset(parents):
    assert await names_of(SoftDeleteParent.objects.only_deleted()) == ["B", "D"]
    assert await names_of(SoftDeleteParent.objects.all().only_deleted()) == ["B", "D"]
    assert await names_of(SoftDeleteParent.objects.filter(name__in=["A", "B"]).only_deleted()) == ["B"]
    assert await names_of(SoftDeleteParent.objects.only_deleted().filter(name="D")) == ["D"]


@pytest.mark.asyncio
async def test_only_deleted_rows_carry_their_deletion_timestamp(parents):
    rows = await SoftDeleteParent.objects.only_deleted()
    assert rows
    assert all(row.deleted_at is not None for row in rows)


@pytest.mark.asyncio
async def test_only_deleted_requires_soft_delete_configured(db):
    with pytest.raises(QueryError, match="soft_delete_field"):
        SoftDeleteChildCascadeHard.objects.only_deleted()
    with pytest.raises(QueryError, match="soft_delete_field"):
        Author.objects.all().only_deleted()


@pytest.mark.asyncio
async def test_last_call_wins_between_only_deleted_and_include_deleted(parents):
    assert await names_of(SoftDeleteParent.objects.only_deleted().include_deleted()) == ["A", "B", "C", "D"]
    assert await names_of(SoftDeleteParent.objects.include_deleted().only_deleted()) == ["B", "D"]
    assert await names_of(SoftDeleteParent.objects.only_deleted().only_deleted()) == ["B", "D"]
    assert await names_of(SoftDeleteParent.objects.only_deleted().include_deleted().only_deleted()) == ["B", "D"]


@pytest.mark.asyncio
async def test_only_deleted_keeps_a_user_filter_on_the_same_field(parents):
    """include_deleted() after only_deleted() drops only the only_deleted() filter itself."""
    queryset = (
        SoftDeleteParent.objects.include_deleted().filter(deleted_at__isnull=True).only_deleted().include_deleted()
    )
    assert await names_of(queryset) == ["A", "C"]


@pytest.mark.asyncio
async def test_toggling_on_one_base_queryset_reuses_no_stale_shape(parents):
    """The same base queryset alternated between the three scopes - the query-shape cache must
    never hand one scope's SQL to another."""
    base = SoftDeleteParent.objects.all()
    for __ in range(2):
        assert await names_of(base) == ["A", "C"]
        assert await names_of(base.only_deleted()) == ["B", "D"]
        assert await names_of(base.include_deleted()) == ["A", "B", "C", "D"]
        assert await names_of(base.only_deleted().filter(name="B")) == ["B"]
        assert await names_of(base.filter(name="B")) == []


@pytest.mark.asyncio
async def test_only_deleted_get_first_count_exists(parents):
    assert (await SoftDeleteParent.objects.only_deleted().get(name="B")).name == "B"
    with pytest.raises(DoesNotExist):
        await SoftDeleteParent.objects.only_deleted().get(name="A")
    assert (await SoftDeleteParent.objects.only_deleted().order_by("name").first()).name == "B"
    assert await SoftDeleteParent.objects.only_deleted().count() == 2
    assert await SoftDeleteParent.objects.only_deleted().filter(name="B").exists()
    assert not await SoftDeleteParent.objects.only_deleted().filter(name="A").exists()


@pytest.mark.asyncio
async def test_only_deleted_values_and_values_list(parents):
    assert await SoftDeleteParent.objects.only_deleted().order_by("name").values_list("name", flat=True) == ["B", "D"]
    assert await SoftDeleteParent.objects.only_deleted().order_by("name").values("name") == [
        {"name": "B"},
        {"name": "D"},
    ]


@pytest.mark.asyncio
async def test_only_deleted_update_touches_only_deleted_rows(parents):
    assert await SoftDeleteParent.objects.only_deleted().update(name="restored-name") == 2
    assert await names_of(SoftDeleteParent.objects.include_deleted()) == ["A", "C", "restored-name", "restored-name"]
    assert await names_of(SoftDeleteParent.objects.all()) == ["A", "C"]


@pytest.mark.asyncio
async def test_restore_everything_only_deleted_returns(parents):
    for row in await SoftDeleteParent.objects.only_deleted():
        await row.restore()
    assert await SoftDeleteParent.objects.only_deleted().count() == 0
    assert await names_of(SoftDeleteParent.objects.all()) == ["A", "B", "C", "D"]


@pytest.mark.asyncio
async def test_only_deleted_through_a_related_manager(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteChildCascadeSoft.objects.create(name="live", parent=parent)
    removed = await SoftDeleteChildCascadeSoft.objects.create(name="removed", parent=parent)
    await removed.delete()
    other_parent = await SoftDeleteParent.objects.create(name="Other")
    other_removed = await SoftDeleteChildCascadeSoft.objects.create(name="other-removed", parent=other_parent)
    await other_removed.delete()

    assert await names_of(parent.cascade_children.all().only_deleted()) == ["removed"]
    assert await names_of(parent.cascade_children.filter(name="removed").only_deleted()) == ["removed"]
    assert await names_of(parent.cascade_children.all()) == ["live"]


@pytest.mark.asyncio
async def test_only_deleted_prefetch_sees_cascade_deleted_children(db):
    """A soft-delete cascade soft-deletes the children too - prefetching from an only_deleted()
    parent finds them, same as include_deleted() does."""
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=parent)
    await parent.delete()

    (deleted_parent,) = await SoftDeleteParent.objects.only_deleted().prefetch_related("cascade_children")
    assert [child.name for child in deleted_parent.cascade_children] == ["child"]


@pytest.mark.asyncio
async def test_only_deleted_select_related_and_relation_filter_to_a_deleted_target(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=parent)
    await parent.delete()

    (child,) = await SoftDeleteChildCascadeSoft.objects.only_deleted().select_related("parent")
    assert child.parent.name == "P"
    assert await SoftDeleteChildCascadeSoft.objects.only_deleted().filter(parent__name="P").count() == 1
    assert await SoftDeleteChildCascadeSoft.objects.only_deleted().values_list("parent__name", flat=True) == ["P"]


@pytest.mark.asyncio
async def test_only_deleted_keeps_the_tenant_filter(db):
    with Tenancy.scope(1):
        await TenantScopedFactory.objects.create(name="Live", company_id=1)
        own_deleted = await TenantScopedFactory.objects.create(name="OwnDeleted", company_id=1)
        await own_deleted.delete()
    with Tenancy.scope(2):
        foreign_deleted = await TenantScopedFactory.objects.create(name="ForeignDeleted", company_id=2)
        await foreign_deleted.delete()

    with Tenancy.scope(1):
        assert await names_of(TenantScopedFactory.objects.only_deleted()) == ["OwnDeleted"]
        assert await names_of(TenantScopedFactory.objects.all().only_deleted()) == ["OwnDeleted"]
        assert await TenantScopedFactory.objects.only_deleted().count() == 1
    with Tenancy.scope(2):
        assert await names_of(TenantScopedFactory.objects.only_deleted()) == ["ForeignDeleted"]

    assert await names_of(TenantScopedFactory.objects.all_tenants().only_deleted()) == ["ForeignDeleted", "OwnDeleted"]
    with Tenancy.scope(1):
        assert await names_of(TenantScopedFactory.objects.only_deleted().all_tenants()) == [
            "ForeignDeleted",
            "OwnDeleted",
        ]


@pytest.mark.asyncio
async def test_only_deleted_awaited_without_active_tenant_raises(db):
    queryset = TenantScopedFactory.objects.only_deleted()
    with pytest.raises(QueryError, match="no tenant is active"):
        await queryset


# ============================================================================
# count()/exists()/update()/delete() built from an .include_deleted()/.all_tenants() queryset
# used to drop that escape hatch for a relation they JOIN - the joined row was scoped by the
# default filter again, so count() said 0 while awaiting the same queryset returned the row.
# ============================================================================


@pytest_asyncio.fixture
async def child_of_deleted_parent(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=parent)
    await parent.delete()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_method", ["include_deleted", "only_deleted"])
async def test_derived_queries_keep_the_soft_delete_escape_across_a_join(child_of_deleted_parent, scope_method):
    queryset = getattr(SoftDeleteChildCascadeSoft.objects, scope_method)().filter(parent__name="P")
    assert len(await queryset) == 1
    assert await queryset.count() == 1
    assert await queryset.exists()
    assert await queryset.update(name="renamed") == 1
    # The already soft-deleted child is left alone by delete(); a live child of the deleted parent
    # is still reached through the JOIN, but only include_deleted() matches live rows at all.
    parent = await SoftDeleteParent.objects.include_deleted().get(name="P")
    live_child = await SoftDeleteChildCascadeSoft.objects.create(name="live", parent=parent)
    reaches_live_rows = scope_method == "include_deleted"
    assert await queryset.delete() == (1 if reaches_live_rows else 0)
    live_child_after = await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=live_child.pk)
    assert (live_child_after.deleted_at is not None) == reaches_live_rows


@pytest.mark.asyncio
async def test_derived_queries_keep_all_tenants_across_a_join(db):
    with Tenancy.scope(2):
        factory = await TenantScopedFactory.objects.create(name="Foreign", company_id=2)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    await TenantScopedWidget.objects.create(name="W", company_id=1, factory=factory)
    with Tenancy.scope(1):
        queryset = TenantScopedWidget.objects.all_tenants().filter(factory__name="Foreign")
        assert len(await queryset) == 1
        assert await queryset.count() == 1
        assert await queryset.exists()
        assert await queryset.update(name="W2") == 1


@pytest.mark.parametrize("model", [SoftDeleteStandalone, SoftDeleteParent])
@pytest.mark.parametrize("scope", ["include_deleted", "only_deleted"])
@pytest.mark.asyncio
async def test_bulk_delete_keeps_the_original_deletion_time_of_already_deleted_rows(db, model, scope):
    """Bug: QuerySet.delete() through include_deleted()/only_deleted() re-stamped deleted_at on
    rows that were already soft-deleted (and counted them), losing their original deletion time -
    unlike Model.delete(), which leaves an already-deleted instance alone. Covers both the
    single-UPDATE fast path (no incoming relations) and the per-instance path."""
    already_deleted = await model.objects.create(name="old")
    await already_deleted.delete()
    original_deleted_at = (await model.objects.include_deleted().get(pk=already_deleted.pk)).deleted_at
    live = await model.objects.create(name="live")

    queryset = getattr(model.objects.all(), scope)()
    deleted_count = await queryset.delete()

    assert deleted_count == (1 if scope == "include_deleted" else 0)
    assert (await model.objects.include_deleted().get(pk=already_deleted.pk)).deleted_at == original_deleted_at
    live_deleted_at = (await model.objects.include_deleted().get(pk=live.pk)).deleted_at
    assert (live_deleted_at is not None) == (scope == "include_deleted")
