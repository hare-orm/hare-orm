import asyncio
import os
import sys
import types
import uuid

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib import test as hare_test
from hare.contrib.test import requires_features
from hare.dialects.enums import DialectName
from hare.exceptions import (
    ConfigurationError,
    IntegrityError,
    QueryError,
    UnSupportedError,
)
from hare.fields import CASCADE, RESTRICT, SET_NULL
from hare.models import Model
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Q, Subquery
from hare.query.functions import Count
from hare.query.queryset import QuerySet
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    ActiveAuthor,
    Author,
    SharedTopic,
    TenantActiveAuthor,
    TenantActiveAuthorBook,
    TenantActiveAuthorNote,
    TenantArticle,
    TenantFkCompany,
    TenantFkNamedOrder,
    TenantFkOrder,
    TenantScopedAsyncDefault,
    TenantScopedCascadeSoftDept,
    TenantScopedCascadeSoftOrg,
    TenantScopedFactory,
    TenantScopedOrder,
    TenantScopedTag,
    TenantScopedWidget,
    TenantTeam,
    TenantTeamMember,
    TenantTeamMembership,
    UuidTenantDoc,
    UuidTenantLabel,
)
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.multi_database_context import MultiDatabaseTestContext

# ============================================================================
# Tenancy.scope() contextvar behavior - nesting, reset (no db needed)
# ============================================================================


def test_current_tenant_scope_sets_the_active_tenant():
    assert Tenancy.current.get() is None
    with Tenancy.scope(1):
        assert Tenancy.current.get() == 1
    assert Tenancy.current.get() is None


def test_current_tenant_scope_resets_on_exception():
    with pytest.raises(ValueError):
        with Tenancy.scope(1):
            raise ValueError("boom")
    assert Tenancy.current.get() is None


def test_current_tenant_scope_nesting_does_not_leak():
    with Tenancy.scope(1):
        with Tenancy.scope(2):
            assert Tenancy.current.get() == 2
        assert Tenancy.current.get() == 1
    assert Tenancy.current.get() is None


def test_set_and_reset_current_tenant_directly():
    token = Tenancy.set(1)
    try:
        assert Tenancy.current.get() == 1
    finally:
        Tenancy.reset(token)
    assert Tenancy.current.get() is None


# ============================================================================
# Default-manager auto-filter, all_tenants() escape hatch, no-active-scope guard
# ============================================================================


@pytest.mark.asyncio
async def test_filter_and_all_auto_scope_to_the_active_tenant(db):
    await TenantScopedWidget.objects.create(name="A1", company_id=1)
    await TenantScopedWidget.objects.create(name="A2", company_id=1)
    await TenantScopedWidget.objects.create(name="B1", company_id=2)

    with Tenancy.scope(1):
        names = {w.name for w in await TenantScopedWidget.objects.all()}
        assert names == {"A1", "A2"}
        assert {w.name for w in await TenantScopedWidget.objects.filter()} == {"A1", "A2"}

    with Tenancy.scope(2):
        names = {w.name for w in await TenantScopedWidget.objects.all()}
        assert names == {"B1"}


@pytest.mark.asyncio
async def test_queries_alternating_between_tenants_see_each_tenant_s_rows(db):
    """The visibility resolved for a tenant and the description of its filters are kept for the next
    query - a query of another tenant, of no tenant, or of every tenant between them reads its own."""
    await TenantScopedWidget.objects.create(name="A1", company_id=1)
    await TenantScopedWidget.objects.create(name="B1", company_id=2)
    await TenantScopedWidget.objects.create(name="B2", company_id=2)
    for _ in range(3):
        with Tenancy.scope(1):
            assert [widget.name for widget in await TenantScopedWidget.objects.filter(name__startswith="")] == ["A1"]
            assert (await TenantScopedWidget.objects.get(name="A1")).company_id == 1
        with Tenancy.scope(2):
            assert sorted(widget.name for widget in await TenantScopedWidget.objects.filter(name__startswith="")) == [
                "B1",
                "B2",
            ]
            assert await TenantScopedWidget.objects.filter(name="A1").first() is None
        with Tenancy.scope(Tenancy.any_of(1, 2)):
            assert await TenantScopedWidget.objects.filter(name__startswith="").count() == 3
        assert await TenantScopedWidget.objects.all_tenants().filter(name__startswith="").count() == 3
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.filter(name__startswith="")


@pytest.mark.asyncio
async def test_get_auto_scopes_to_the_active_tenant(db):
    same_tenant = await TenantScopedWidget.objects.create(name="Mine", company_id=1)
    other_tenant = await TenantScopedWidget.objects.create(name="Theirs", company_id=2)

    with Tenancy.scope(1):
        fetched = await TenantScopedWidget.objects.get(pk=same_tenant.pk)
        assert fetched.name == "Mine"

        from hare.exceptions import DoesNotExist

        with pytest.raises(DoesNotExist):
            await TenantScopedWidget.objects.get(pk=other_tenant.pk)


@pytest.mark.asyncio
async def test_single_tenant_in_scope_basic_sanity(db):
    """No cross-tenant rows in play at all - the common single-tenant-in-scope case still just
    behaves like an ordinary queryset."""
    await TenantScopedWidget.objects.create(name="Only", company_id=1)

    with Tenancy.scope(1):
        widgets = await TenantScopedWidget.objects.all()
        assert len(widgets) == 1
        assert widgets[0].name == "Only"
        assert await TenantScopedWidget.objects.filter(name="Only").exists()


@pytest.mark.asyncio
async def test_all_tenants_bypasses_scoping(db):
    await TenantScopedWidget.objects.create(name="A1", company_id=1)
    await TenantScopedWidget.objects.create(name="B1", company_id=2)

    names = {w.name for w in await TenantScopedWidget.objects.all_tenants()}
    assert names == {"A1", "B1"}


@pytest.mark.asyncio
async def test_all_tenants_on_a_model_without_tenant_field_changes_nothing_of_its_own(db):
    author = await Author.objects.create(name="A")
    assert await Author.objects.all_tenants().values_list("id", flat=True) == [author.id]


@pytest.mark.asyncio
async def test_querying_without_an_active_tenant_scope_raises(db):
    await TenantScopedWidget.objects.create(name="A1", company_id=1)

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.all()
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.filter(name="A1")


@pytest.mark.asyncio
async def test_update_with_tenant_field_kwarg_raises(db):
    await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.filter(name="A1").update(company_id=2)


# ============================================================================
# bulk_update() cross-tenant security guard
# ============================================================================


@pytest.mark.asyncio
async def test_bulk_update_cross_tenant_object_raises(db):
    same_tenant = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    other_tenant = await TenantScopedWidget.objects.create(name="B1", company_id=2)

    same_tenant.name = "A1-renamed"
    other_tenant.name = "B1-renamed"

    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.bulk_update([same_tenant, other_tenant], fields=["name"])

    # Neither row was touched - the guard runs before any write.
    refreshed_same = await TenantScopedWidget.objects.all_tenants().get(pk=same_tenant.pk)
    refreshed_other = await TenantScopedWidget.objects.all_tenants().get(pk=other_tenant.pk)
    assert refreshed_same.name == "A1"
    assert refreshed_other.name == "B1"


@pytest.mark.asyncio
async def test_bulk_update_same_tenant_objects_succeeds(db):
    first = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    second = await TenantScopedWidget.objects.create(name="A2", company_id=1)

    first.name = "A1-renamed"
    second.name = "A2-renamed"

    with Tenancy.scope(1):
        await TenantScopedWidget.objects.bulk_update([first, second], fields=["name"])

        names = {w.name for w in await TenantScopedWidget.objects.all()}
        assert names == {"A1-renamed", "A2-renamed"}


@pytest.mark.asyncio
async def test_bulk_update_with_no_active_tenant_scope_raises(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    widget.name = "renamed"

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.bulk_update([widget], fields=["name"])


@pytest.mark.asyncio
async def test_bulk_update_bare_constructed_forged_pk_cross_tenant_hijack_does_not_mutate_the_row(db):
    """Same forged-bare-construction gap as save()'s own equivalent test: the existing
    mismatched_pks check compares the active tenant only against the forged instance's own
    in-memory tenant_field attribute, trivially satisfiable since the caller sets that
    attribute itself - the row the forged pk actually names belongs to a different tenant."""
    victim = await TenantScopedWidget.objects.create(name="victim-original", company_id=2)

    with Tenancy.scope(1):
        forged = TenantScopedWidget(id=victim.pk, name="hijacked", company_id=1)
        forged._saved_in_db = True
        await TenantScopedWidget.objects.bulk_update([forged], fields=["name"])

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=victim.pk)
    assert refreshed.name == "victim-original"


@pytest.mark.asyncio
async def test_low_level_queryset_bulk_update_bare_constructed_forged_pk_does_not_hijack(db):
    """Same gap as test_bulk_update_bare_constructed_forged_pk_cross_tenant_hijack_does_not_mutate_the_row,
    reached directly through the low-level QuerySet.bulk_update() by bypassing the Model.objects.bulk_update()
    facade entirely - see test_low_level_queryset_bulk_update_still_guards_when_bypassing_manager_entirely."""
    victim = await TenantScopedWidget.objects.create(name="victim-original-2", company_id=2)

    with Tenancy.scope(1):
        forged = TenantScopedWidget(id=victim.pk, name="hijacked-2", company_id=1)
        forged._saved_in_db = True
        await QuerySet(TenantScopedWidget).bulk_update([forged], fields=["name"])

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=victim.pk)
    assert refreshed.name == "victim-original-2"


@pytest.mark.asyncio
async def test_all_tenants_bulk_update_bypasses_the_facade_and_still_succeeds(db):
    """Model.objects.all_tenants().bulk_update(...) reaches the low-level QuerySet.bulk_update()
    directly, bypassing Model.objects.bulk_update()'s own facade guard entirely (all_tenants() returns a
    bare QuerySet) - the low-level method needs its own equivalent guard that still recognizes
    this as a deliberate cross-tenant escape hatch, not an accidental one."""
    same_tenant = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    other_tenant = await TenantScopedWidget.objects.create(name="B1", company_id=2)
    same_tenant.name = "A1-renamed"
    other_tenant.name = "B1-renamed"

    await TenantScopedWidget.objects.all_tenants().bulk_update([same_tenant, other_tenant], fields=["name"])

    names = {w.name for w in await TenantScopedWidget.objects.all_tenants()}
    assert names == {"A1-renamed", "B1-renamed"}


# ============================================================================
# ManyToManyRelation.add() cross-tenant guard - had no tenant scoping at all before this fix,
# unlike every other write path in this codebase.
# ============================================================================


@pytest.mark.asyncio
async def test_m2m_add_rejects_a_related_object_from_a_different_tenant(db):
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    other_tenant_tag = await TenantScopedTag.objects.create(name="T-other", company_id=2)

    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await widget.tags.add(other_tenant_tag)

    assert await TenantScopedTag.objects.all_tenants().filter(widgets=widget.pk).count() == 0


@pytest.mark.asyncio
async def test_m2m_add_rejects_the_owning_instance_from_a_different_tenant(db):
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    tag = await TenantScopedTag.objects.create(name="T1", company_id=2)

    with Tenancy.scope(2):
        with pytest.raises(QueryError):
            await widget.tags.add(tag)


@pytest.mark.asyncio
async def test_m2m_add_succeeds_for_same_tenant_objects_on_both_sides(db):
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    tag = await TenantScopedTag.objects.create(name="T1", company_id=1)

    with Tenancy.scope(1):
        await widget.tags.add(tag)
        assert [t.name for t in await widget.tags] == ["T1"]


@pytest.mark.asyncio
async def test_m2m_add_requires_an_active_tenant_scope(db):
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    tag = await TenantScopedTag.objects.create(name="T1", company_id=1)

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await widget.tags.add(tag)


@pytest.mark.asyncio
async def test_m2m_add_rejects_a_bare_constructed_forged_owning_instance_with_a_cross_tenant_pk(db):
    """Same forged-bare-construction gap as save()'s own equivalent test: the previous
    in-memory-only check (getattr(instance, tenant_field) != active_tenant) was trivially
    satisfiable by a forged owning instance whose tenant_field the caller sets to their own
    active tenant, while the pk it claims actually names a different tenant's real row - which
    would otherwise gain a relation the victim's real tenant never consented to."""
    victim_widget = await TenantScopedWidget.objects.create(name="victim-widget", company_id=2)

    with Tenancy.scope(1):
        own_tag = await TenantScopedTag.objects.create(name="tenant1-tag", company_id=1)
        forged_widget = TenantScopedWidget(id=victim_widget.pk, name="whatever", company_id=1)
        forged_widget._saved_in_db = True
        with pytest.raises(QueryError):
            await forged_widget.tags.add(own_tag)

    with Tenancy.scope(2):
        real = await TenantScopedWidget.objects.get(pk=victim_widget.pk)
        assert await real.tags.all().count() == 0


@pytest.mark.asyncio
async def test_m2m_remove_and_clear_reject_a_bare_constructed_forged_owning_instance(db):
    """remove()/clear() had NO tenant check at all before this fix, unlike add()'s own (even if
    previously bypassable) _check_tenant_scope - nothing stopped removing/soft-deleting a
    through-table row linking a DIFFERENT tenant's rows just because the caller happened to
    hold a Python reference to them."""
    victim_widget = await TenantScopedWidget.objects.create(name="victim-widget-2", company_id=2)
    with Tenancy.scope(2):
        real_tag = await TenantScopedTag.objects.create(name="tenant2-tag", company_id=2)
        await victim_widget.tags.add(real_tag)

    with Tenancy.scope(1):
        forged_widget = TenantScopedWidget(id=victim_widget.pk, name="whatever", company_id=1)
        forged_widget._saved_in_db = True
        with pytest.raises(QueryError):
            await forged_widget.tags.clear()
        with pytest.raises(QueryError):
            await forged_widget.tags.remove(real_tag)

    with Tenancy.scope(2):
        real = await TenantScopedWidget.objects.get(pk=victim_widget.pk)
        assert await real.tags.all().count() == 1


@pytest.mark.asyncio
async def test_bulk_update_rejects_unresolved_async_default_tenant_field(db):
    """tenant_field's own async default= callable leaves it pending in _await_when_save until
    save() resolves it - bulk_update() never calls save(), so an object constructed straight
    from Model(...) and never saved/fetched used to crash the tenant-mismatch check with a raw
    AttributeError instead of raising a clear, actionable ConfigurationError."""
    obj = TenantScopedAsyncDefault(id=1, name="A")
    assert "company_id" in obj._await_when_save

    with Tenancy.scope(1):
        with pytest.raises(QueryError, match="unresolved async default"):
            await TenantScopedAsyncDefault.objects.bulk_update([obj], fields=["name"])


@pytest.mark.asyncio
async def test_low_level_queryset_bulk_update_rejects_unresolved_async_default_tenant_field(db):
    """Same check, reached directly through the low-level QuerySet.bulk_update() by bypassing
    Manager entirely - see test_low_level_queryset_bulk_update_still_guards_when_bypassing_manager_entirely."""
    obj = TenantScopedAsyncDefault(id=1, name="A")

    with Tenancy.scope(1):
        with pytest.raises(QueryError, match="unresolved async default"):
            QuerySet(TenantScopedAsyncDefault).bulk_update([obj], fields=["name"])


@pytest.mark.asyncio
async def test_low_level_queryset_bulk_update_still_guards_when_bypassing_manager_entirely(db):
    """A bare QuerySet(Model), built by bypassing Manager (and .all_tenants()) entirely, is as
    close as Python code can get to calling the low-level method directly - it raises when run
    with no tenant active, exactly like the facade."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    widget.name = "renamed"

    assert Tenancy.current.get() is None
    query = QuerySet(TenantScopedWidget).bulk_update([widget], fields=["name"])
    with pytest.raises(QueryError):
        await query


@pytest.mark.asyncio
async def test_bulk_create_with_no_active_tenant_scope_raises(db):
    """bulk_update() already re-checked Meta.tenant_field at the low-level QuerySet method, in
    case a caller reaches it without going through Model.objects.bulk_create()'s own facade guard -
    bulk_create() had no such defense-in-depth check at all."""
    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.bulk_create([TenantScopedWidget(name="A1", company_id=1)])


@pytest.mark.asyncio
async def test_all_tenants_bulk_create_bypasses_the_facade_and_still_succeeds(db):
    """Model.objects.all_tenants().bulk_create(...) reaches the low-level QuerySet.bulk_create()
    directly, bypassing Model.objects.bulk_create()'s own facade guard entirely (all_tenants() returns a
    bare QuerySet) - the same escape hatch bulk_update() already accounts for at this layer."""
    same_tenant = TenantScopedWidget(name="A1", company_id=1)
    other_tenant = TenantScopedWidget(name="B1", company_id=2)

    await TenantScopedWidget.objects.all_tenants().bulk_create([same_tenant, other_tenant])

    names = {w.name for w in await TenantScopedWidget.objects.all_tenants()}
    assert names == {"A1", "B1"}


@pytest.mark.asyncio
async def test_low_level_queryset_bulk_create_still_guards_when_bypassing_manager_entirely(db):
    """A bare QuerySet(Model), built by bypassing Manager (and .all_tenants()) entirely, is as
    close as Python code can get to calling the low-level method directly - it raises when run
    with no tenant active, exactly like the facade."""
    assert Tenancy.current.get() is None
    query = QuerySet(TenantScopedWidget).bulk_create([TenantScopedWidget(name="A1", company_id=1)])
    with pytest.raises(QueryError):
        await query


@pytest.mark.asyncio
async def test_bulk_create_auto_fills_unset_tenant_field_at_low_level(db):
    """The low-level QuerySet.bulk_create() guard mirrors the facade's own auto-fill behavior
    for an object that doesn't carry a tenant value yet, not just the reject-on-mismatch half."""
    with Tenancy.scope(1):
        widget = TenantScopedWidget(name="A1")
        # company_id's type stub says int (never None) since the field isn't null=True - true
        # once saved, but this widget hasn't been yet, so it's genuinely still None at runtime.
        assert widget.company_id is None

        await QuerySet(TenantScopedWidget).bulk_create([widget])  # type: ignore[unreachable]

        assert widget.company_id == 1


# ============================================================================
# save() cross-tenant write guard (round-2 finding: save() had NO Meta.tenant_field check at
# all, unlike every other write path above - instance.tenant_id = other_tenant_id;
# await instance.save() silently hijacked the row into a different tenant)
# ============================================================================


@pytest.mark.asyncio
async def test_save_cross_tenant_hijack_raises(db):
    """The exact reported PoC: reassigning tenant_field on an already-loaded instance and
    calling save() must not silently move the row into a different tenant."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        widget.company_id = 2
        with pytest.raises(QueryError):
            await widget.save()

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk)
    assert refreshed.company_id == 1


@pytest.mark.asyncio
async def test_save_bare_constructed_forged_pk_cross_tenant_hijack_does_not_mutate_the_row(db):
    """Unlike test_save_cross_tenant_hijack_raises's reassignment on an already-fetched instance,
    this instance is never fetched at all: Model(id=<victim's real pk>, tenant_field=<own
    active tenant>, ...) satisfies save()'s Python-side consistency check trivially (nothing to
    compare the claimed tenant_field against except itself), while the row that pk actually
    names belongs to a different tenant. The UPDATE's own WHERE clause must independently
    confine the write to the active tenant, so this either raises (0 rows affected) or - if a
    row for the SAME pk genuinely exists under the active tenant instead - never touches the
    other tenant's row."""
    victim = await TenantScopedWidget.objects.create(name="victim-original", company_id=2)

    with Tenancy.scope(1):
        forged = TenantScopedWidget(id=victim.pk, name="hijacked", company_id=1)
        forged._saved_in_db = False
        with pytest.raises(IntegrityError):
            await forged.save(update_fields=["name"])

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=victim.pk)
    assert refreshed.name == "victim-original"


@pytest.mark.asyncio
async def test_save_update_under_a_different_active_scope_than_the_loaded_row_raises(db):
    """A row loaded under tenant 1, then saved while a DIFFERENT tenant's scope is active
    (without the caller ever touching tenant_field itself) must also raise - reusing a
    stale, cross-tenant-loaded instance is exactly as dangerous as reassigning the field."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(2):
        widget.name = "renamed"
        with pytest.raises(QueryError):
            await widget.save()


@pytest.mark.asyncio
async def test_save_update_within_the_matching_tenant_scope_succeeds(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        widget.name = "renamed"
        await widget.save()

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk)
    assert refreshed.name == "renamed"


@pytest.mark.asyncio
async def test_save_partial_update_not_touching_tenant_field_skips_the_check(db):
    """save(update_fields=[...]) that doesn't name tenant_field has nothing to validate for it -
    a partial instance that never loaded tenant_field at all must not be forced to."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        partial = await TenantScopedWidget.objects.filter(pk=widget.pk).only("id", "name").first()
        assert partial is not None
        partial.name = "renamed"
        await partial.save(update_fields=["name"])

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk)
    assert refreshed.name == "renamed"
    assert refreshed.company_id == 1


@pytest.mark.asyncio
async def test_save_new_instance_auto_injects_tenant_when_unset(db):
    """A new instance built directly (Model(...)) rather than through create() must be
    auto-populated the same way create()/bulk_create() already are."""
    with Tenancy.scope(1):
        widget = TenantScopedWidget(name="A1")
        await widget.save()

    assert widget.company_id == 1
    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk)
    assert refreshed.company_id == 1


@pytest.mark.asyncio
async def test_save_new_instance_with_mismatched_tenant_raises(db):
    with Tenancy.scope(1):
        widget = TenantScopedWidget(name="A1", company_id=999)
        with pytest.raises(QueryError):
            await widget.save()


@pytest.mark.asyncio
async def test_save_update_with_no_active_scope_but_tenant_already_set_is_trusted(db):
    """Mirrors create()'s own documented semantics exactly (create() delegates to
    save(force_create=True)): a value already set on the instance with NO active scope at all is
    trusted as-is - e.g. seed/fixture data, or a script that never bothers with Tenancy.scope()
    but passes tenant info directly via the object."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)
    widget.name = "renamed"

    assert Tenancy.current.get() is None
    await widget.save()

    refreshed = await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk)
    assert refreshed.name == "renamed"


@pytest.mark.asyncio
async def test_save_new_instance_without_scope_and_without_tenant_raises(db):
    """Mirrors test_create_without_scope_and_without_tenant_kwarg_raises exactly - nothing to
    fall back on for tenant_field either way."""
    assert Tenancy.current.get() is None
    widget = TenantScopedWidget(name="X")
    with pytest.raises(QueryError):
        await widget.save()


# ============================================================================
# create()/get_or_create()/bulk_create() cross-tenant write guard
# ============================================================================


@pytest.mark.asyncio
async def test_create_inside_scope_with_mismatched_tenant_kwarg_raises(db):
    """The exact cross-tenant-write PoC: create() must not silently accept a tenant value that
    conflicts with the active scope."""
    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.create(name="X", company_id=999)

    # Nothing was written under either tenant.
    assert {w.name for w in await TenantScopedWidget.objects.all_tenants()} == set()


@pytest.mark.asyncio
async def test_create_inside_scope_without_tenant_kwarg_auto_injects(db):
    with Tenancy.scope(1):
        widget = await TenantScopedWidget.objects.create(name="X")
        assert widget.company_id == 1


@pytest.mark.asyncio
async def test_create_without_scope_and_without_tenant_kwarg_raises(db):
    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.create(name="X")


@pytest.mark.asyncio
async def test_get_or_create_cross_tenant_via_read_miss_now_raises(db):
    """The second cross-tenant-write PoC: a get_or_create() whose explicit tenant kwarg
    conflicts with the active scope always misses on the read (the scope's own filter ANDs with
    the unsatisfiable kwarg) and used to fall through to an unguarded create() - now raises
    instead of silently writing into the wrong tenant."""
    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.get_or_create(company_id=2, name="X")

    assert {w.name for w in await TenantScopedWidget.objects.all_tenants()} == set()


@pytest.mark.asyncio
async def test_get_or_create_same_tenant_still_works(db):
    with Tenancy.scope(1):
        widget, created = await TenantScopedWidget.objects.get_or_create(name="X", company_id=1)
        assert created is True
        assert widget.company_id == 1

        widget2, created2 = await TenantScopedWidget.objects.get_or_create(name="X", company_id=1)
        assert created2 is False
        assert widget2.pk == widget.pk


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real, persistent connection - Transactions.autonomous() opens a genuinely separate
    connection per task, needed for the concurrent race test below to have two independent
    connections actually racing at the DB level, not two calls sharing one connection's
    transaction state (which asyncpg outright forbids two concurrent operations on)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    db_url = os.getenv("HARE_TEST_DB")
    if not db_url or DatabaseUnderTest.is_file_database(db_url):
        scheme = (db_url or DatabaseUnderTest.DEFAULT_URL).split("://", 1)[0]
        db_path = tmp_path / "tenancy_race_test.sqlite"
        db_url = f"{scheme}:///{db_path}?synchronous=OFF"
    async with hare_test_context(["tests.testmodels"], db_url=db_url, connection_label="models") as ctx:
        yield ctx


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_get_or_create_concurrent_race_on_same_key_no_duplicate(file_db):
    """Regression test for _create_or_get() keeping its explicit transaction wrapper (a plain
    INSERT alone is atomic, but the wrapper's SAVEPOINT semantics matter once nested inside an
    already-open outer transaction - see that method's own comment) - two genuinely concurrent
    get_or_create() calls, each on its OWN connection via Transactions.autonomous(), racing on
    the same unique key must still resolve to exactly one row, with exactly one of them
    reporting created=True. Postgres-only: SQLite serializes writers hard enough across
    independent connections that the two tasks never actually overlap, so the race never
    reproduces there (same reasoning as test_optimistic_lock_field_concurrent_saves_via_autonomous_one_wins
    in test_optimistic_locking.py)."""
    barrier = asyncio.Barrier(2)

    async def attempt() -> tuple[TenantScopedWidget, bool]:
        async with Transactions.autonomous() as conn:
            await barrier.wait()
            with Tenancy.scope(1):
                return await TenantScopedWidget.objects.using(conn).get_or_create(name="race", company_id=1)

    with Tenancy.scope(1):
        results = await asyncio.gather(attempt(), attempt())
        created_flags = sorted(created for _, created in results)
        assert created_flags == [False, True]
        assert results[0][0].pk == results[1][0].pk

        matches = await TenantScopedWidget.objects.filter(name="race")
        assert len(matches) == 1


@pytest.mark.asyncio
async def test_bulk_create_cross_tenant_object_raises(db):
    other_tenant_widget = TenantScopedWidget(name="B1", company_id=2)

    with Tenancy.scope(1):
        with pytest.raises(QueryError):
            await TenantScopedWidget.objects.bulk_create([other_tenant_widget])

    assert {w.name for w in await TenantScopedWidget.objects.all_tenants()} == set()


@pytest.mark.asyncio
async def test_bulk_create_auto_injects_tenant_when_unset(db):
    widgets = [TenantScopedWidget(name="A1"), TenantScopedWidget(name="A2")]

    with Tenancy.scope(1):
        await TenantScopedWidget.objects.bulk_create(widgets)
        names = {w.name for w in await TenantScopedWidget.objects.all()}
        assert names == {"A1", "A2"}
        assert all(w.company_id == 1 for w in widgets)


@pytest.mark.asyncio
async def test_bulk_create_with_no_active_tenant_scope_and_no_tenant_set_raises(db):
    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.bulk_create([TenantScopedWidget(name="A1")])


@pytest.mark.asyncio
async def test_bulk_create_update_fields_rejects_tenant_field(db):
    """ON CONFLICT resolves against the table's physical unique constraint/pk regardless of
    tenant scoping - update_fields=[tenant_field] would let a caller active under THEIR OWN
    tenant silently move an EXISTING row (matched purely by that physical constraint, e.g. its
    id) into their own tenant. Real cross-tenant data corruption, not just a raw driver error -
    must be rejected up front, the same as targeting a generated field."""
    with Tenancy.scope(1):
        victim = await TenantScopedWidget.objects.create(name="victim")

    with Tenancy.scope(2):
        with pytest.raises(QueryError, match="company_id"):
            await TenantScopedWidget.objects.bulk_create(
                [TenantScopedWidget(id=victim.id, name="victim", company_id=2)],
                on_conflict=("id",),
                update_fields=["company_id"],
            )

    with Tenancy.scope(1):
        assert (await TenantScopedWidget.objects.get(id=victim.id)).company_id == 1


# ============================================================================
# delete()/restore() cross-tenant write guard
# ============================================================================


@pytest.mark.asyncio
async def test_delete_cross_tenant_raises(db):
    """An instance loaded/created under one tenant must not be deletable while a DIFFERENT
    tenant's scope is active - same hijack risk save() already guards against."""
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(2):
        with pytest.raises(QueryError):
            await widget.delete()

    assert await TenantScopedWidget.objects.all_tenants().get(pk=widget.pk) is not None


@pytest.mark.asyncio
async def test_delete_within_matching_tenant_scope_succeeds(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        await widget.delete()

    assert await TenantScopedWidget.objects.all_tenants().filter(pk=widget.pk).first() is None


@pytest.mark.asyncio
async def test_delete_bare_constructed_forged_pk_cross_tenant_hijack_does_not_soft_delete_the_row(db):
    """Same forged-bare-construction gap as save()'s own equivalent test, but for delete()'s
    soft-delete UPDATE path (TenantScopedFactory has Meta.soft_delete_field set) - the
    Python-side _check_active_tenant_scope_for_write() check compares only the forged
    instance's own tenant_field attribute, never the real row in the database."""
    victim = await TenantScopedFactory.objects.create(name="victim-factory", company_id=2)

    with Tenancy.scope(1):
        forged = TenantScopedFactory(id=victim.pk, name="whatever", company_id=1)
        forged._saved_in_db = True
        with pytest.raises(IntegrityError):
            await forged.delete()

    refreshed = await TenantScopedFactory.objects.all_tenants().get(pk=victim.pk)
    assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_delete_with_no_active_scope_but_tenant_already_set_is_trusted(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    assert Tenancy.current.get() is None
    await widget.delete()

    assert await TenantScopedWidget.objects.all_tenants().filter(pk=widget.pk).first() is None


@pytest.mark.asyncio
async def test_restore_cross_tenant_raises(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        await factory.delete()

    with Tenancy.scope(2):
        with pytest.raises(QueryError):
            await factory.restore()

    with Tenancy.scope(1):
        names = {f.name for f in await TenantScopedFactory.objects.all()}
        assert names == set()


@pytest.mark.asyncio
async def test_restore_within_matching_tenant_scope_succeeds(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        await factory.delete()
        await factory.restore()

        names = {f.name for f in await TenantScopedFactory.objects.all()}
        assert names == {"F1"}


# ============================================================================
# refresh_from_db() respects Meta.tenant_field (Finding 2)
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_from_db_raises_when_unscoped(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await widget.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_from_db_raises_does_not_exist_for_a_different_tenants_row(db):
    from hare.exceptions import DoesNotExist

    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(2):
        with pytest.raises(DoesNotExist):
            await widget.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_from_db_succeeds_for_the_active_tenants_own_row(db):
    widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)

    with Tenancy.scope(1):
        await TenantScopedWidget.objects.filter(pk=widget.pk).update(name="Renamed")
        await widget.refresh_from_db()
        assert widget.name == "Renamed"


# ============================================================================
# all_tenants()/include_deleted() drop only their own filter, chainable (Finding 3)
# ============================================================================


@pytest.mark.asyncio
async def test_all_tenants_alone_still_filters_out_soft_deleted_rows(db):
    with Tenancy.scope(1):
        await TenantScopedFactory.objects.create(name="Visible", company_id=1)
        deleted = await TenantScopedFactory.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
    with Tenancy.scope(2):
        await TenantScopedFactory.objects.create(name="Other", company_id=2)

    names = {f.name for f in await TenantScopedFactory.objects.all_tenants()}
    assert names == {"Visible", "Other"}


@pytest.mark.asyncio
async def test_include_deleted_alone_still_filters_by_the_active_tenant(db):
    with Tenancy.scope(1):
        await TenantScopedFactory.objects.create(name="Visible", company_id=1)
        deleted = await TenantScopedFactory.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
    with Tenancy.scope(2):
        other_tenant_deleted = await TenantScopedFactory.objects.create(name="OtherDeleted", company_id=2)
        await other_tenant_deleted.delete()

    with Tenancy.scope(1):
        names = {f.name for f in await TenantScopedFactory.objects.include_deleted()}
        assert names == {"Visible", "Deleted"}


@pytest.mark.asyncio
async def test_all_tenants_include_deleted_chained_bypasses_both_filters(db):
    with Tenancy.scope(1):
        await TenantScopedFactory.objects.create(name="Visible", company_id=1)
        deleted = await TenantScopedFactory.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
    with Tenancy.scope(2):
        other_tenant_deleted = await TenantScopedFactory.objects.create(name="OtherDeleted", company_id=2)
        await other_tenant_deleted.delete()

    names = {f.name for f in await TenantScopedFactory.objects.all_tenants().include_deleted()}
    assert names == {"Visible", "Deleted", "OtherDeleted"}


@pytest.mark.asyncio
async def test_include_deleted_requires_soft_delete_field_configured(db):
    with pytest.raises(QueryError):
        Author.objects.include_deleted()


# ============================================================================
# all_tenants()/include_deleted() must propagate across a relation-crossing JOIN/prefetch,
# not just filter the base table - a bug found live: the base table correctly returned rows
# across every tenant, but the JOINED/prefetched TenantScopedFactory side silently re-applied
# the single currently-active tenant/soft-delete scoping, either dropping the related row
# (None instead of the real factory) or raising ConfigurationError from the escape hatch's own
# declared side of the query.
# ============================================================================


@pytest.mark.asyncio
async def test_all_tenants_propagates_across_select_related_join(db):
    with Tenancy.scope(1):
        factory1 = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        widget1 = await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory1)
    with Tenancy.scope(2):
        factory2 = await TenantScopedFactory.objects.create(name="F2", company_id=2)
        await TenantScopedWidget.objects.create(name="W2", company_id=2, factory=factory2)

    with Tenancy.scope(1):
        widgets = {
            w.name: w
            for w in await TenantScopedWidget.objects.all_tenants().select_related("factory").order_by("name")
        }
    assert widgets.keys() == {"W1", "W2"}
    assert widgets["W1"].factory is not None
    assert widgets["W1"].factory.name == "F1"
    # The real bug: this used to come back None (or raise, in the no-active-tenant variant
    # below) because the JOIN re-applied Tenancy.current (1) to the related table, even though
    # the base query declared all_tenants().
    assert widgets["W2"].factory is not None
    assert widgets["W2"].factory.name == "F2"
    assert widget1.id  # keeps the fixture object referenced/used


@pytest.mark.asyncio
async def test_all_tenants_select_related_works_with_no_active_tenant_scope(db):
    with Tenancy.scope(1):
        factory1 = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory1)

    # No Tenancy.scope() active at all here - the whole point of calling all_tenants(). This
    # used to raise ConfigurationError from inside the JOIN's own ambient scoping on
    # TenantScopedFactory, even though the caller explicitly declared all_tenants() to avoid
    # exactly that.
    widgets = list(await TenantScopedWidget.objects.all_tenants().select_related("factory"))
    assert len(widgets) == 1
    assert widgets[0].factory is not None
    assert widgets[0].factory.name == "F1"


@pytest.mark.asyncio
async def test_all_tenants_propagates_across_nested_filter_through_relation(db):
    with Tenancy.scope(1):
        factory1 = await TenantScopedFactory.objects.create(name="Shared", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory1)
    with Tenancy.scope(2):
        factory2 = await TenantScopedFactory.objects.create(name="Shared", company_id=2)
        await TenantScopedWidget.objects.create(name="W2", company_id=2, factory=factory2)

    with Tenancy.scope(1):
        names = {
            w.name
            for w in await TenantScopedWidget.objects.all_tenants()
            .select_related("factory")
            .filter(factory__name="Shared")
        }
    # Used to silently drop W2 - the nested factory__name=... filter built its own JOIN scoped
    # to the currently-active tenant only, regardless of all_tenants() on the base query.
    assert names == {"W1", "W2"}


@pytest.mark.asyncio
async def test_all_tenants_include_deleted_propagates_across_prefetch_related(db):
    with Tenancy.scope(1):
        factory1 = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory1)
    with Tenancy.scope(2):
        factory2 = await TenantScopedFactory.objects.create(name="F2", company_id=2)
        deleted_factory2 = await TenantScopedFactory.objects.create(name="F2Deleted", company_id=2)
        await deleted_factory2.delete()
        await TenantScopedWidget.objects.create(name="W2", company_id=2, factory=factory2)

    with Tenancy.scope(1):
        factories = {
            f.name: f
            for f in await TenantScopedFactory.objects.all_tenants().include_deleted().prefetch_related("widgets")
        }
    assert factories.keys() == {"F1", "F2", "F2Deleted"}
    # The real bug: f2.widgets used to come back [] (a bare TenantScopedWidget.objects.all() prefetch
    # query enforcing only the single currently-active tenant), silently, with no error at all.
    assert [w.name for w in factories["F2"].widgets] == ["W2"]
    assert list(factories["F2Deleted"].widgets) == []


# ============================================================================
# .raw() must be consistently opaque to Meta.tenant_field scoping - never requiring an active
# Tenancy.scope() (like a fully-scoped query would) while also never actually applying it (like
# a fully-raw query already doesn't) - that half-scoped combination was the real bug.
# ============================================================================


@pytest.mark.asyncio
async def test_raw_does_not_require_an_active_tenant_scope(db):
    """Used to raise ConfigurationError here (the same "no tenant active" guard .all()/.filter()
    correctly apply), even though the raw SQL itself was never going to be scoped by it either
    way - .raw() must be fully opaque to Meta.tenant_field, not gate on it without using it."""
    await TenantScopedWidget.objects.create(name="A1", company_id=1)
    assert Tenancy.current.get() is None
    rows = await TenantScopedWidget.objects.raw("SELECT * FROM tenantscopedwidget")
    assert [w.name for w in rows] == ["A1"]


@pytest.mark.asyncio
async def test_raw_does_not_apply_tenant_scoping_even_when_a_tenant_is_active(db):
    """The real bug: with a tenant active, .raw() computed the ambient tenant filter (via
    Manager.get_queryset(), the same machinery .all()/.filter() use) purely to satisfy its own
    "is a tenant active" gate, then silently discarded the filter itself and ran fully unscoped -
    a tenant-1 caller's raw query leaked tenant-2's rows through with no error or warning at all."""
    await TenantScopedWidget.objects.create(name="T1-Widget", company_id=1)
    await TenantScopedWidget.objects.create(name="T2-Widget", company_id=2)

    with Tenancy.scope(1):
        rows = await TenantScopedWidget.objects.raw("SELECT * FROM tenantscopedwidget ORDER BY name")
        scoped_rows = await TenantScopedWidget.objects.all()

    assert {w.name for w in rows} == {"T1-Widget", "T2-Widget"}
    assert {w.name for w in scoped_rows} == {"T1-Widget"}


# ============================================================================
# CASCADE/RESTRICT/SET_NULL delete-time checks must see a related row regardless of which
# tenant happens to be ambiently active - a row physically referencing the instance being
# deleted exists regardless of the CALLER's own Tenancy.scope(), the same reasoning
# _include_soft_deleted() already established for Meta.soft_delete_field.
# ============================================================================

CASCADE_TENANT_MODULE_NAME = "tests._cascade_tenant_models"


@pytest_asyncio.fixture
async def cascade_tenant_models():
    """A tenant-scoped parent/child pair linked by an UNCONSTRAINED (db_constraint=False) FK -
    isolates hare's own Python-side cascade machinery (ReverseRelationCascade) from the
    database's own real FK constraint, which would otherwise backstop (SET_NULL/CASCADE) or
    block (RESTRICT) the same scenario regardless of whether hare's own Python pre-check ever
    saw the related row at all."""

    class CTParent(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        company_id = fields.IntField()

        class Meta:
            app = "cascade_tenant"
            tenant_field = "company_id"

    class CTChildSetNull(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        company_id = fields.IntField()
        parent: fields.ForeignKeyNullableRelation[CTParent] = fields.ForeignKeyField(
            "cascade_tenant.CTParent",
            related_name="set_null_children",
            null=True,
            on_delete=SET_NULL,
            db_constraint=False,
        )

        class Meta:
            app = "cascade_tenant"
            tenant_field = "company_id"

    class CTChildRestrict(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        company_id = fields.IntField()
        parent: fields.ForeignKeyRelation[CTParent] = fields.ForeignKeyField(
            "cascade_tenant.CTParent",
            related_name="restrict_children",
            on_delete=RESTRICT,
            db_constraint=False,
        )

        class Meta:
            app = "cascade_tenant"
            tenant_field = "company_id"

    class CTChildCascade(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        company_id = fields.IntField()
        parent: fields.ForeignKeyRelation[CTParent] = fields.ForeignKeyField(
            "cascade_tenant.CTParent",
            related_name="cascade_children",
            on_delete=CASCADE,
            db_constraint=False,
        )

        class Meta:
            app = "cascade_tenant"
            tenant_field = "company_id"

    module = types.ModuleType(CASCADE_TENANT_MODULE_NAME)
    setattr(module, "CTParent", CTParent)  # noqa: B010
    setattr(module, "CTChildSetNull", CTChildSetNull)  # noqa: B010
    setattr(module, "CTChildRestrict", CTChildRestrict)  # noqa: B010
    setattr(module, "CTChildCascade", CTChildCascade)  # noqa: B010
    sys.modules[CASCADE_TENANT_MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default"],
            apps={"cascade_tenant": {"models": [CASCADE_TENANT_MODULE_NAME], "default_connection": "default"}},
        ) as ctx:
            await ctx.generate_schemas()
            yield CTParent, CTChildSetNull, CTChildRestrict, CTChildCascade
    finally:
        sys.modules.pop(CASCADE_TENANT_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_cascade_set_null_reaches_a_cross_tenant_child(cascade_tenant_models):
    """The real bug: ReverseRelationCascade._related_query() built its related-row query via
    Model.objects.filter(), which applies the RELATED model's own ambient ._meta.tenant_field scoping
    from whatever Tenancy.scope() the CALLER happens to be in - completely unrelated to which
    tenant the child row it needs to find actually belongs to. Confirmed live before this fix:
    deleting a tenant-1 parent under Tenancy.scope(1) silently left a tenant-2 child's FK
    dangling (parent_id still pointing at a row that no longer exists) instead of SET_NULL-ing
    it, since the cascade's own lookup for tenant-2 children never found any while scoped to
    tenant 1."""
    CTParent, CTChildSetNull, _CTChildRestrict, _CTChildCascade = cascade_tenant_models

    with Tenancy.scope(1):
        parent = await CTParent.objects.create(name="P1", company_id=1)
    # A child in a DIFFERENT tenant than its own parent - not realistic multi-tenant modeling
    # (written with no tenant active, as trusted seed data), but exactly the shape needed to prove
    # the cascade query itself (not just "happens to already be correctly tenant-consistent data")
    # respects tenant scoping.
    child = await CTChildSetNull.objects.create(name="C1", company_id=2, parent_id=parent.id)

    with Tenancy.scope(1):
        await parent.delete()

    with Tenancy.scope(2):
        refreshed_child = await CTChildSetNull.objects.get(pk=child.pk)
    assert refreshed_child.parent_id is None


@pytest.mark.asyncio
async def test_cascade_restrict_blocks_delete_with_a_cross_tenant_child(cascade_tenant_models):
    """Same bug, RESTRICT side: a cross-tenant RESTRICT-guarded child went completely
    undetected, silently letting the delete through instead of raising IntegrityError."""
    CTParent, _CTChildSetNull, CTChildRestrict, _CTChildCascade = cascade_tenant_models

    with Tenancy.scope(1):
        parent = await CTParent.objects.create(name="P1", company_id=1)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    await CTChildRestrict.objects.create(name="C1", company_id=2, parent_id=parent.id)

    with Tenancy.scope(1):
        with pytest.raises(IntegrityError):
            await parent.delete()


@pytest.mark.asyncio
async def test_cascade_hard_delete_removes_a_cross_tenant_cascade_child(cascade_tenant_models):
    """The cascade correctly FINDS a cross-tenant CASCADE child via _tenant_safe_base_query()/
    .all_tenants(), but used to leave it dangling: _persist_hard_delete()'s real DELETE re-applied
    BaseExecutor._tenant_scope_where()'s active-tenant WHERE guard (meant to stop a forged,
    bare-constructed instance from deleting a row it never actually read) to a row the cascade had
    already verified through a real FK match - the DELETE silently matched 0 rows, the parent was
    removed anyway, and the child survived with parent_id still pointing at the now-deleted row."""
    CTParent, _CTChildSetNull, _CTChildRestrict, CTChildCascade = cascade_tenant_models

    with Tenancy.scope(1):
        parent = await CTParent.objects.create(name="P1", company_id=1)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    child = await CTChildCascade.objects.create(name="C1", company_id=2, parent_id=parent.id)

    with Tenancy.scope(1):
        await parent.delete()

    assert not await CTParent.objects.all_tenants().filter(pk=parent.pk).exists()
    assert not await CTChildCascade.objects.all_tenants().filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_cascade_hard_delete_forged_pk_cross_tenant_hijack_still_raises(cascade_tenant_models):
    """apply_active_tenant_scope_guard defaults to True for the instance delete() was directly
    called on (never the cascade-discovered descendants, which set it False) - a bare-constructed
    forged instance must still be unable to delete a row belonging to a different tenant than its
    own claimed company_id, exactly like the existing soft-delete forged-pk regression test."""
    CTParent, _CTChildSetNull, _CTChildRestrict, _CTChildCascade = cascade_tenant_models
    victim = await CTParent.objects.create(name="victim", company_id=2)

    with Tenancy.scope(1):
        forged = CTParent(id=victim.pk, name="whatever", company_id=1)
        forged._saved_in_db = True
        with pytest.raises(IntegrityError):
            await forged.delete()

    assert await CTParent.objects.all_tenants().filter(pk=victim.pk).exists()


@pytest.mark.asyncio
async def test_cascade_set_null_works_under_all_tenants_with_no_active_scope(cascade_tenant_models):
    """A caller deliberately using .all_tenants() (e.g. an admin/background job) with NO active
    Tenancy.scope() at all must not have the cascade crash the instant it reaches a
    tenant-scoped child - the real bug used to raise ConfigurationError here instead of
    completing the delete/SET_NULL correctly."""
    CTParent, CTChildSetNull, _CTChildRestrict, _CTChildCascade = cascade_tenant_models

    with Tenancy.scope(1):
        parent = await CTParent.objects.create(name="P1", company_id=1)
        child = await CTChildSetNull.objects.create(name="C1", company_id=1, parent_id=parent.id)

    assert Tenancy.current.get() is None
    victim = await CTParent.objects.all_tenants().get(pk=parent.pk)
    await victim.delete()

    with Tenancy.scope(1):
        refreshed_child = await CTChildSetNull.objects.get(pk=child.pk)
    assert refreshed_child.parent_id is None


# ============================================================================
# Soft-delete cascade must not re-apply the active-tenant WHERE guard to a descendant it already
# found via .all_tenants() - same underlying bug as the hard-delete CASCADE tests above, but for
# _persist_soft_delete()'s UPDATE instead of _persist_hard_delete()'s DELETE.
# ============================================================================


@pytest.mark.asyncio
async def test_soft_delete_cascade_reaches_a_cross_tenant_child(db):
    """The cascade correctly finds a cross-tenant soft-delete CASCADE child via
    _tenant_safe_base_query()/.all_tenants(), but _persist_soft_delete()'s UPDATE used to
    re-apply the active-tenant WHERE guard to it - the UPDATE matched 0 rows, and
    _persist_soft_delete() raised IntegrityError("Can't delete object that doesn't exist") even
    though the row is very much still there, just in a different tenant. That exception unwound
    the whole delete()'s own transaction, so the parent itself was never soft-deleted either."""
    org = await TenantScopedCascadeSoftOrg.objects.create(name="Org1", company_id=1)
    dept = await TenantScopedCascadeSoftDept.objects.create(name="Dept-other-tenant", company_id=2, org_id=org.id)

    with Tenancy.scope(1):
        await org.delete()

    refreshed_org = await TenantScopedCascadeSoftOrg.objects.all_tenants().include_deleted().get(pk=org.pk)
    refreshed_dept = await TenantScopedCascadeSoftDept.objects.all_tenants().include_deleted().get(pk=dept.pk)
    assert refreshed_org.deleted_at is not None
    assert refreshed_dept.deleted_at is not None


@pytest.mark.asyncio
async def test_soft_delete_cascade_forged_pk_cross_tenant_hijack_still_raises(db):
    """Same forged-bare-construction guard as test_delete_bare_constructed_forged_pk_cross_
    tenant_hijack_does_not_soft_delete_the_row, for a model reached via a CASCADE (not the plain
    single-model delete() that test already covers) - apply_active_tenant_scope_guard must stay
    True for the instance delete() was directly called on, only False for what the cascade itself
    discovers."""
    victim = await TenantScopedCascadeSoftOrg.objects.create(name="victim-org", company_id=2)

    with Tenancy.scope(1):
        forged = TenantScopedCascadeSoftOrg(id=victim.pk, name="whatever", company_id=1)
        forged._saved_in_db = True
        with pytest.raises(IntegrityError):
            await forged.delete()

    refreshed = await TenantScopedCascadeSoftOrg.objects.all_tenants().get(pk=victim.pk)
    assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_m2m_add_fills_through_model_tenant_from_active_scope(db):
    with Tenancy.scope(7):
        team = await TenantTeam.objects.create()
        member = await TenantTeamMember.objects.create()
        await team.members.add(member)
        assert [row.pk for row in await team.members.all()] == [member.pk]
        assert await TenantTeamMembership.objects.all().values_list("company_id", flat=True) == [7]

        await team.members.set(member)
        assert await TenantTeamMembership.objects.all().values_list("company_id", flat=True) == [7]
        with pytest.raises(QueryError, match="differs from the active tenant"):
            await team.members.add(member, through_defaults={"company_id": 8})

        await team.members.remove(member)
        assert await team.members.all() == []
        await team.members.add(member)
        await team.members.clear()
        assert await TenantTeamMembership.objects.all().count() == 0


@pytest.mark.asyncio
async def test_create_derives_fk_shadow_tenant_field_from_the_relation_kwarg(db):
    first_company = await TenantFkCompany.objects.create(name="first")
    second_company = await TenantFkCompany.objects.create(name="second")

    order = await TenantFkOrder.objects.create(company=first_company, title="seed")
    assert order.company_id == first_company.id

    with Tenancy.scope(first_company.id):
        scoped_order = await TenantFkOrder.objects.create(company=first_company, title="own")
        assert scoped_order.company_id == first_company.id
        with pytest.raises(QueryError, match="does not match the active tenant scope"):
            await TenantFkOrder.objects.create(company=second_company, title="foreign")
        with pytest.raises(QueryError, match="Cannot set 'company_id' via .update"):
            await TenantFkOrder.objects.filter(pk=scoped_order.pk).update(company=second_company)

    assert await TenantFkOrder.objects.all_tenants().filter(title="foreign").count() == 0


# ============================================================================
# The tenant is the one active when the query runs - values()/subqueries/prefetch_related()
# ============================================================================


@pytest.mark.asyncio
async def test_values_and_values_list_run_for_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="f1")
        await TenantScopedWidget.objects.create(name="w1", factory=factory)
    widgets = TenantScopedWidget.objects.filter(factory__name="f1")
    with Tenancy.scope(2):
        await TenantScopedFactory.objects.create(name="f1")
        assert await widgets.values_list("name", flat=True) == []
        assert await widgets.count() == 0
    with Tenancy.scope(1):
        assert await widgets.values_list("name", flat=True) == ["w1"]
        assert await widgets.values("name") == [{"name": "w1"}]
        assert await widgets.count() == 1


@pytest.mark.asyncio
async def test_queryset_used_as_a_subquery_runs_for_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="f1")
        widget = await TenantScopedWidget.objects.create(name="w1", factory=factory)
    await TenantScopedOrder.objects.create(name="o1", widget=widget)
    widgets = TenantScopedWidget.objects.filter(factory__name="f1")
    with Tenancy.scope(2):
        assert await TenantScopedOrder.objects.filter(widget__in=widgets) == []
    with Tenancy.scope(1):
        assert [order.name for order in await TenantScopedOrder.objects.filter(widget__in=widgets)] == ["o1"]
        in_values = TenantScopedOrder.objects.filter(widget_id__in=Subquery(widgets.values("id")))
        assert await in_values.values_list("name", flat=True) == ["o1"]


@pytest.mark.asyncio
async def test_prefetch_related_runs_for_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="f1")
        await TenantScopedWidget.objects.create(name="w1", factory=factory)
    factories = TenantScopedFactory.objects.all().prefetch_related("widgets")
    with Tenancy.scope(1):
        assert [[widget.name for widget in item.widgets] for item in await factories] == [["w1"]]
    with Tenancy.scope(2):
        assert await factories == []
    with pytest.raises(QueryError, match="no tenant is active"):
        await factories


# ============================================================================
# bulk_create(update_fields=...) never overwrites another tenant's row
# ============================================================================


@pytest.mark.asyncio
async def test_bulk_create_upsert_does_not_overwrite_another_tenants_row(db):
    with Tenancy.scope(2):
        foreign = await TenantScopedWidget.objects.create(name="foreign")
    with Tenancy.scope(1):
        own = await TenantScopedWidget.objects.create(name="own")
        hijack = TenantScopedWidget(id=foreign.id, name="hijacked", company_id=1)
        update = TenantScopedWidget(id=own.id, name="renamed", company_id=1)
        await TenantScopedWidget.objects.bulk_create([hijack, update], on_conflict=["id"], update_fields=["name"])
        assert not hijack._saved_in_db
        assert await TenantScopedWidget.objects.all().values_list("name", flat=True) == ["renamed"]
    foreign_row = await TenantScopedWidget.objects.all_tenants().get(id=foreign.id).values_list("name", "company_id")
    assert foreign_row == ("foreign", 2)


@pytest.mark.asyncio
async def test_bulk_create_upsert_returning_skips_another_tenants_row(db):
    if TenantScopedWidget.get_connection().dialect.name != DialectName.POSTGRESQL:
        pytest.skip("bulk_create(returning=True) is Postgres-only")
    with Tenancy.scope(2):
        foreign = await TenantScopedWidget.objects.create(name="foreign")
    with Tenancy.scope(1):
        own = await TenantScopedWidget.objects.create(name="own")
        hijack = TenantScopedWidget(id=foreign.id, name="hijacked")
        update = TenantScopedWidget(id=own.id, name="renamed")
        fresh = TenantScopedWidget(id=own.id + foreign.id + 100, name="fresh")
        await TenantScopedWidget.objects.bulk_create(
            [hijack, update, fresh], on_conflict=["id"], update_fields=["name"], returning=True
        )
        assert not hijack._saved_in_db
        assert update._saved_in_db
        assert fresh._saved_in_db
        assert sorted(await TenantScopedWidget.objects.all().values_list("name", flat=True)) == ["fresh", "renamed"]
    assert (
        await TenantScopedWidget.objects.all_tenants().get(id=foreign.id).values_list("name", flat=True) == "foreign"
    )


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_bulk_create_upsert_sql_limits_do_update_to_the_active_tenant(db):
    with Tenancy.scope(1):
        sql = TenantScopedWidget.objects.bulk_create(
            [TenantScopedWidget(id=5, name="x")], on_conflict=["id"], update_fields=["name"]
        ).sql(parameters_inline=True)
    assert sql.endswith('WHERE "tenantscopedwidget"."company_id"=1')
    unrestricted = (
        TenantScopedWidget.objects.all_tenants()
        .bulk_create([TenantScopedWidget(id=5, name="x", company_id=1)], on_conflict=["id"], update_fields=["name"])
        .sql(parameters_inline=True)
    )
    assert "WHERE" not in unrestricted


@pytest.mark.asyncio
async def test_bulk_create_upsert_explicit_returning_without_on_conflict_columns_raises(db):
    with Tenancy.scope(1), pytest.raises(QueryError, match="requires on_conflict"):
        TenantScopedWidget.objects.bulk_create(
            [TenantScopedWidget(name="x")],
            on_conflict_constraint="some_constraint",
            update_fields=["name"],
            returning=True,
        )


# ============================================================================
# Custom no-parameter Manager.get_queryset() with tenant/soft-delete escape hatches
# ============================================================================


@pytest.mark.asyncio
async def test_model_escape_hatches_work_through_a_custom_no_parameter_manager(db):
    with Tenancy.scope(1):
        live = await TenantActiveAuthor.objects.create(name="live")
        deleted = await TenantActiveAuthor.objects.create(name="deleted")
        await TenantActiveAuthor.objects.create(name="inactive", is_active=False)
        await deleted.delete()
    with Tenancy.scope(2):
        await TenantActiveAuthor.objects.create(name="other")
        assert sorted(await TenantActiveAuthor.objects.all_tenants().values_list("name", flat=True)) == [
            "live",
            "other",
        ]
    with Tenancy.scope(1):
        assert sorted(await TenantActiveAuthor.objects.include_deleted().values_list("name", flat=True)) == [
            "deleted",
            "live",
        ]
        assert await TenantActiveAuthor.objects.only_deleted().values_list("name", flat=True) == ["deleted"]
        await live.refresh_from_db()
        await deleted.refresh_from_db()
        assert deleted.deleted_at is not None


@pytest.mark.asyncio
async def test_refresh_from_db_works_through_a_custom_no_parameter_manager(db):
    author = await ActiveAuthor.objects.create(name="before")
    await ActiveAuthor.objects.filter(pk=author.pk).update(name="after")
    await author.refresh_from_db()
    assert author.name == "after"


@pytest.mark.asyncio
async def test_delete_cascade_reaches_a_model_with_a_custom_no_parameter_manager(db):
    with Tenancy.scope(1):
        org = await TenantScopedCascadeSoftOrg.objects.create(name="org")
        await TenantActiveAuthorNote.objects.create(text="note", org=org)
        preview = await org.delete_preview()
        assert preview.soft_deleted[TenantActiveAuthorNote] == 1
        await org.delete()
        assert await TenantActiveAuthorNote.objects.all().count() == 0
        assert await TenantActiveAuthorNote.objects.only_deleted().count() == 1


# ============================================================================
# UUIDField tenant values compare after the field's own conversion
# ============================================================================


@pytest.mark.asyncio
async def test_uuid_tenant_m2m_add_remove_clear_and_set_accept_the_same_tenant(db):
    org = uuid.uuid4()
    for scope_value in (org, str(org)):
        with Tenancy.scope(scope_value):
            doc = await UuidTenantDoc.objects.create(title=f"doc-{type(scope_value).__name__}")
            label = await UuidTenantLabel.objects.create(name=f"label-{type(scope_value).__name__}")
            await doc.labels.add(label)
            assert [item.pk for item in await doc.labels.all()] == [label.pk]
            await doc.labels.remove(label)
            await doc.labels.set(label)
            await doc.labels.clear()
            assert await doc.labels.all() == []
            doc.title = "renamed"
            await doc.save()
            await doc.delete()


@pytest.mark.asyncio
async def test_uuid_tenant_given_as_string_matches_on_create_and_bulk_writes(db):
    org = uuid.uuid4()
    with Tenancy.scope(str(org)):
        doc = await UuidTenantDoc.objects.create(title="doc", org=org)
        await UuidTenantDoc.objects.bulk_update([doc], fields=["title"])
        await UuidTenantDoc.objects.bulk_create([UuidTenantDoc(title="bulk", org=org)])
    with Tenancy.scope(org):
        assert sorted(await UuidTenantDoc.objects.all().values_list("title", flat=True)) == ["bulk", "doc"]


# ============================================================================
# Soft-delete cascade through an auto-through M2M ignores the active tenant scope
# ============================================================================


@pytest.mark.asyncio
async def test_soft_delete_with_auto_through_m2m_links_needs_no_active_scope(db):
    org = uuid.uuid4()
    with Tenancy.scope(org):
        doc = await UuidTenantDoc.objects.create(title="doc")
        await doc.labels.add(await UuidTenantLabel.objects.create(name="label"))
    loaded = await UuidTenantDoc.objects.all_tenants().get(pk=doc.pk)
    await loaded.delete()
    with Tenancy.scope(org):
        assert await UuidTenantDoc.objects.only_deleted().count() == 1
        label = await UuidTenantLabel.objects.get(name="label")
        assert await label.docs.all() == []


@pytest.mark.asyncio
async def test_soft_delete_cascade_clears_m2m_links_of_another_tenants_row(db):
    first_org, second_org = uuid.uuid4(), uuid.uuid4()
    with Tenancy.scope(first_org):
        parent = await UuidTenantDoc.objects.create(title="parent")
    # No tenant active: a cross-tenant link is written as trusted seed data.
    child = await UuidTenantDoc.objects.create(title="child", org=second_org, parent_id=parent.pk)
    with Tenancy.scope(second_org):
        await child.labels.add(await UuidTenantLabel.objects.create(name="label"))
    with Tenancy.scope(first_org):
        preview = await parent.delete_preview()
        assert preview.can_delete
        assert preview.many_to_many_through == {UuidTenantDoc._meta.fields_map["labels"].through: 1}
        await parent.delete()
    with Tenancy.scope(second_org):
        assert await UuidTenantDoc.objects.only_deleted().values_list("title", flat=True) == ["child"]
        label = await UuidTenantLabel.objects.get(name="label")
        assert await label.docs.all() == []


# ============================================================================
# all_tenants().delete() inside an active scope
# ============================================================================


@pytest.mark.asyncio
async def test_all_tenants_delete_inside_a_scope_deletes_another_tenants_rows_on_both_paths(db):
    with Tenancy.scope(2):
        org = await TenantScopedCascadeSoftOrg.objects.create(name="org")
        await TenantScopedCascadeSoftDept.objects.create(name="dept", org=org)
        lone_org = await TenantScopedCascadeSoftOrg.objects.create(name="lone-org")
        await TenantScopedCascadeSoftDept.objects.create(name="lone", org=lone_org)
    with Tenancy.scope(1):
        assert await TenantScopedCascadeSoftOrg.objects.all_tenants().filter(name="org").delete() == 1
        assert await TenantScopedCascadeSoftDept.objects.all_tenants().filter(name="lone").delete() == 1
    assert await TenantScopedCascadeSoftOrg.objects.all_tenants().only_deleted().values_list("name", flat=True) == [
        "org"
    ]
    deleted_departments = (
        await TenantScopedCascadeSoftDept.objects.all_tenants().only_deleted().values_list("name", flat=True)
    )
    assert sorted(deleted_departments) == ["dept", "lone"]


# ============================================================================
# M2M __isnull honors the related model's tenant/soft-delete scope
# ============================================================================


@pytest.mark.asyncio
async def test_m2m_isnull_only_counts_live_links_of_the_active_tenant(db):
    org = uuid.uuid4()

    async def titles(queryset):
        return sorted(doc.title for doc in await queryset)

    with Tenancy.scope(org):
        linked = await UuidTenantDoc.objects.create(title="linked")
        await UuidTenantDoc.objects.create(title="empty")
        deleted_link = await UuidTenantDoc.objects.create(title="deleted-link")
        mixed = await UuidTenantDoc.objects.create(title="mixed")
        live_label = await UuidTenantLabel.objects.create(name="live")
        dead_label = await UuidTenantLabel.objects.create(name="dead")
        await UuidTenantLabel.objects.create(name="unused")
        await linked.labels.add(live_label)
        await deleted_link.labels.add(dead_label)
        await mixed.labels.add(live_label, dead_label)
        await dead_label.delete()

        assert await titles(UuidTenantDoc.objects.filter(labels__isnull=False)) == ["linked", "mixed"]
        assert await titles(UuidTenantDoc.objects.filter(labels__isnull=True)) == ["deleted-link", "empty"]
        assert await titles(UuidTenantDoc.objects.filter(labels__not_isnull=True)) == ["linked", "mixed"]
        assert await titles(UuidTenantDoc.objects.filter(labels__not_isnull=False)) == ["deleted-link", "empty"]
        assert await titles(UuidTenantDoc.objects.exclude(labels__isnull=True)) == ["linked", "mixed"]
        assert await titles(UuidTenantDoc.objects.exclude(labels__isnull=False)) == ["deleted-link", "empty"]
        assert await titles(UuidTenantDoc.objects.filter(Q(labels__isnull=True) | Q(title="linked"))) == [
            "deleted-link",
            "empty",
            "linked",
        ]
        assert sorted(await UuidTenantLabel.objects.filter(docs__isnull=True).values_list("name", flat=True)) == [
            "unused"
        ]
        assert await UuidTenantDoc.objects.filter(labels__isnull=False).count() == 2
        with pytest.raises(UnSupportedError, match="expects a bool"):
            await UuidTenantDoc.objects.filter(labels__isnull="yes")
    with Tenancy.scope(uuid.uuid4()):
        await UuidTenantDoc.objects.create(title="foreign-owner")
        assert await titles(UuidTenantDoc.objects.filter(labels__isnull=True)) == ["foreign-owner"]


@pytest.mark.asyncio
async def test_forward_isnull_treats_a_target_of_another_tenant_as_no_target(db):
    """select_related()/values()/await read another tenant's target as None, while
    widget__isnull=True didn't match such a row - only a soft-deleted target was folded in."""
    own_widget = await TenantScopedWidget.objects.create(name="Own", company_id=1)
    other_widget = await TenantScopedWidget.objects.create(name="Other", company_id=2)
    own_order = await TenantScopedOrder.objects.create(name="own", widget=own_widget)
    other_order = await TenantScopedOrder.objects.create(name="other", widget=other_widget)
    no_widget_order = await TenantScopedOrder.objects.create(name="none", widget=None)

    with Tenancy.scope(1):
        read_widgets = {
            order.pk: order.widget for order in await TenantScopedOrder.objects.all().select_related("widget")
        }
        assert read_widgets[other_order.pk] is None
        assert set(await TenantScopedOrder.objects.filter(widget__isnull=True).values_list("id", flat=True)) == {
            other_order.pk,
            no_widget_order.pk,
        }
        assert set(await TenantScopedOrder.objects.filter(widget=None).values_list("id", flat=True)) == {
            other_order.pk,
            no_widget_order.pk,
        }
        assert await TenantScopedOrder.objects.filter(widget__isnull=False).values_list("id", flat=True) == [
            own_order.pk
        ]
        assert set(await TenantScopedOrder.objects.filter(widget_id__isnull=True).values_list("id", flat=True)) == {
            no_widget_order.pk
        }
    with Tenancy.scope(2):
        assert set(await TenantScopedOrder.objects.filter(widget__isnull=True).values_list("id", flat=True)) == {
            own_order.pk,
            no_widget_order.pk,
        }


@pytest.mark.asyncio
async def test_tenant_field_naming_the_foreign_key_uses_its_column(db):
    """Meta.tenant_field = "company" (the relation, not company_id) used to fail at runtime with a
    raw KeyError - it now means the relation's own key column."""
    assert TenantFkNamedOrder._meta.tenant_field == "company_id"
    first_company = await TenantFkCompany.objects.create(id=1, name="first")
    second_company = await TenantFkCompany.objects.create(id=2, name="second")

    with Tenancy.scope(1):
        created = await TenantFkNamedOrder.objects.create(id=1, title="scoped")
        assert created.company_id == 1
        await TenantFkNamedOrder.objects.create(id=2, title="explicit", company=first_company)
        with pytest.raises(QueryError):
            await TenantFkNamedOrder.objects.create(id=3, title="mismatch", company=second_company)
        with pytest.raises(QueryError):
            await TenantFkNamedOrder.objects.filter(id=1).update(company=second_company)
    with Tenancy.scope(2):
        assert await TenantFkNamedOrder.objects.all().count() == 0
    assert sorted(await TenantFkNamedOrder.objects.all_tenants().values_list("id", flat=True)) == [1, 2]


@pytest.mark.asyncio
async def test_tenant_field_without_a_column_is_rejected():
    from hare.contrib.test.isolated_contexts import hare_test_context

    with pytest.raises(ConfigurationError, match="no column of its own"):
        async with hare_test_context(["tests.model_setup.model_tenant_field_m2m"]):
            pass


# ============================================================================
# A bare M2M relation filter (and clear()/set()) from the unscoped side honours the related
# model's tenant/soft-delete scope.
# ============================================================================


@pytest_asyncio.fixture
async def topic_linked_to_two_tenants(db) -> tuple[SharedTopic, TenantArticle, TenantArticle]:
    topic = await SharedTopic.objects.create(id=1, name="shared")
    with Tenancy.scope(1):
        own_article = await TenantArticle.objects.create(id=1, title="own")
        await topic.articles.add(own_article)
    with Tenancy.scope(2):
        other_tenant_article = await TenantArticle.objects.create(id=2, title="other-tenant")
        await topic.articles.add(other_tenant_article)
    return topic, own_article, other_tenant_article


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "value", "expected_names"),
    [
        ("articles", 2, []),
        ("articles__in", [2], []),
        ("articles__not", 1, []),
        ("articles__not_in", [1], []),
        ("articles", 1, ["shared"]),
        ("articles__in", [1, 2], ["shared"]),
    ],
)
async def test_bare_m2m_filter_ignores_links_to_another_tenants_rows(
    topic_linked_to_two_tenants, lookup, value, expected_names
):
    with Tenancy.scope(1):
        assert await SharedTopic.objects.filter(**{lookup: value}).values_list("name", flat=True) == expected_names


@pytest.mark.asyncio
async def test_bare_m2m_exclude_ignores_links_to_another_tenants_rows(topic_linked_to_two_tenants):
    lonely_topic = await SharedTopic.objects.create(id=2, name="lonely")
    with Tenancy.scope(1):
        assert sorted(await SharedTopic.objects.exclude(articles=2).values_list("name", flat=True)) == [
            "lonely",
            "shared",
        ]
        assert await SharedTopic.objects.exclude(articles=1).values_list("name", flat=True) == [lonely_topic.name]
        assert sorted(await SharedTopic.objects.exclude(articles__in=[2]).values_list("name", flat=True)) == [
            "lonely",
            "shared",
        ]


@pytest.mark.asyncio
async def test_bare_m2m_filter_ignores_links_to_soft_deleted_rows(topic_linked_to_two_tenants):
    topic, own_article, _ = topic_linked_to_two_tenants
    connection = TenantArticle._meta.connection
    await connection.execute(
        f'UPDATE "{TenantArticle._meta.db_table}" SET "deleted_at" = CURRENT_TIMESTAMP WHERE "id" = {own_article.pk}'
    )
    with Tenancy.scope(1):
        assert await SharedTopic.objects.filter(articles=own_article.pk).count() == 0
        assert await SharedTopic.objects.filter(articles__in=[own_article.pk]).count() == 0
        assert await SharedTopic.objects.exclude(articles=own_article.pk).values_list("name", flat=True) == [
            topic.name
        ]


@pytest.mark.asyncio
async def test_m2m_clear_from_the_unscoped_side_keeps_another_tenants_links(topic_linked_to_two_tenants):
    topic, _, other_tenant_article = topic_linked_to_two_tenants
    with Tenancy.scope(1):
        await topic.articles.clear()
        assert await topic.articles.all().count() == 0
    with Tenancy.scope(2):
        assert await SharedTopic.objects.filter(articles=other_tenant_article.pk).values_list("name", flat=True) == [
            "shared"
        ]


@pytest.mark.asyncio
async def test_m2m_set_from_the_unscoped_side_keeps_another_tenants_links(topic_linked_to_two_tenants):
    topic, own_article, other_tenant_article = topic_linked_to_two_tenants
    with Tenancy.scope(1):
        replacement_article = await TenantArticle.objects.create(id=3, title="replacement")
        await topic.articles.set(replacement_article)
        assert await topic.articles.all().values_list("id", flat=True) == [replacement_article.pk]
    with Tenancy.scope(2):
        assert await topic.articles.all().values_list("id", flat=True) == [other_tenant_article.pk]
    assert own_article.pk not in await TenantArticle.objects.all_tenants().filter(topics=topic.pk).values_list(
        "id", flat=True
    )


@pytest.mark.asyncio
async def test_unscoped_model_relation_query_without_tenant_needs_all_tenants(topic_linked_to_two_tenants):
    await SharedTopic.objects.create(id=2, name="lonely")
    with pytest.raises(QueryError):
        await SharedTopic.objects.filter(articles__isnull=True).count()
    assert await SharedTopic.objects.all_tenants().filter(articles__isnull=True).values_list("name", flat=True) == [
        "lonely"
    ]
    assert await SharedTopic.objects.all_tenants().filter(articles__title="other-tenant").values_list(
        "name", flat=True
    ) == ["shared"]
    assert await SharedTopic.objects.all_tenants().annotate(article_count=Count("articles")).order_by(
        "id"
    ).values_list("article_count", flat=True) == [2, 0]


@pytest.mark.asyncio
async def test_m2m_clear_all_tenants_from_the_unscoped_side_without_a_tenant(topic_linked_to_two_tenants):
    topic, _, _ = topic_linked_to_two_tenants
    with pytest.raises(QueryError):
        await topic.articles.clear()
    await topic.articles.clear(all_tenants=True)
    assert await SharedTopic.objects.all_tenants().filter(articles__isnull=False).count() == 0


@pytest_asyncio.fixture
async def factories_of_two_tenants(db):
    with Tenancy.scope(1):
        own_factory = await TenantScopedFactory.objects.create(name="own", company_id=1)
    with Tenancy.scope(2):
        foreign_factory = await TenantScopedFactory.objects.create(name="foreign", company_id=2)
    return own_factory, foreign_factory


@pytest.mark.asyncio
async def test_forward_relation_to_another_tenants_row_is_rejected_on_create(factories_of_two_tenants):
    own_factory, foreign_factory = factories_of_two_tenants
    with Tenancy.scope(1):
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.create(name="by instance", factory=foreign_factory)
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.create(name="by key", factory_id=foreign_factory.id)
        await TenantScopedWidget.objects.create(name="own", factory=own_factory)
        await TenantScopedWidget.objects.create(name="none")
        assert await TenantScopedWidget.objects.all().order_by("name").values_list("name", flat=True) == [
            "none",
            "own",
        ]


@pytest.mark.asyncio
async def test_forward_relation_to_another_tenants_row_is_rejected_on_save_and_update(factories_of_two_tenants):
    own_factory, foreign_factory = factories_of_two_tenants
    with Tenancy.scope(1):
        widget = await TenantScopedWidget.objects.create(name="W", factory=own_factory)
        widget.factory = foreign_factory
        with pytest.raises(QueryError, match="can't cross tenants"):
            await widget.save()
        with pytest.raises(QueryError, match="can't cross tenants"):
            await widget.save(update_fields=["factory"])
        widget.name = "renamed"
        # A partial save not writing the relation isn't checked.
        await widget.save(update_fields=["name"])
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.filter(id=widget.id).update(factory_id=foreign_factory.id)
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.filter(id=widget.id).update(factory=foreign_factory)
        assert await TenantScopedWidget.objects.filter(id=widget.id).update(factory=None) == 1
        assert await TenantScopedWidget.objects.filter(id=widget.id).values_list("factory_id", flat=True) == [None]


@pytest.mark.asyncio
async def test_forward_relation_to_another_tenants_row_is_rejected_in_bulk_writes(factories_of_two_tenants):
    own_factory, foreign_factory = factories_of_two_tenants
    with Tenancy.scope(1):
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.bulk_create(
                [
                    TenantScopedWidget(name="a", factory=own_factory),
                    TenantScopedWidget(name="b", factory=foreign_factory),
                ]
            )
        assert not await TenantScopedWidget.objects.all().exists()
        await TenantScopedWidget.objects.bulk_create([TenantScopedWidget(name="a", factory=own_factory)])
        widget = await TenantScopedWidget.objects.get(name="a")
        widget.factory = foreign_factory
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantScopedWidget.objects.bulk_update([widget], fields=["factory"])
        assert await TenantScopedWidget.objects.all().values_list("factory_id", flat=True) == [own_factory.id]


@pytest.mark.asyncio
async def test_shared_model_relation_to_another_tenants_row_is_rejected_in_a_scope(db):
    with Tenancy.scope(2):
        foreign_author = await TenantActiveAuthor.objects.create(name="foreign", company_id=2)
    with Tenancy.scope(1):
        with pytest.raises(QueryError, match="can't cross tenants"):
            await TenantActiveAuthorBook.objects.create(title="book", author=foreign_author)
    # With no tenant active the link is trusted, as seed data.
    await TenantActiveAuthorBook.objects.create(title="seed", author_id=foreign_author.id)
    assert await TenantActiveAuthorBook.objects.all().values_list("title", flat=True) == ["seed"]


@pytest.mark.asyncio
async def test_all_tenants_update_may_point_a_relation_across_tenants(factories_of_two_tenants):
    own_factory, foreign_factory = factories_of_two_tenants
    with Tenancy.scope(1):
        widget = await TenantScopedWidget.objects.create(name="W", factory=own_factory)
        assert await TenantScopedWidget.objects.all_tenants().filter(id=widget.id).update(factory=foreign_factory) == 1
    assert await TenantScopedWidget.objects.all_tenants().values_list("factory_id", flat=True) == [foreign_factory.id]
