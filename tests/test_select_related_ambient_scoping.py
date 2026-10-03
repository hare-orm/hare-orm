import pytest

from hare.contrib import test
from hare.exceptions import (
    QueryError,
)
from hare.models.tenancy import Tenancy
from hare.query.expressions import F, Q, Window
from hare.query.functions import Count as AggregateCount
from hare.query.functions.window import NthValue, Sum as WindowSum
from hare.query.plans.statement_plans import StatementPlans
from hare.query.relation_loading.select import Select
from tests.testmodels import TenantScopedFactory, TenantScopedOrder, TenantScopedWidget

# ============================================================================
# select_related()'s AND .only()'s JOIN paths must each apply the SAME Meta.tenant_field/
# Meta.soft_delete_field scoping the related model's own Manager.get_queryset() would - a plain
# JOIN ON criterion built purely from FK/PK equality would otherwise silently pull in a related
# row belonging to ANY tenant/soft-delete state, regardless of the current scope. The two paths
# build their JOINs through different code (_join_select_related() vs _get_only()), so each needs
# its own coverage - see test_only_scopes_join_to_the_active_tenant and its siblings below.
# ============================================================================


@pytest.mark.asyncio
async def test_select_related_scopes_join_to_the_active_tenant(db):
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        orders = {o.name: o for o in await TenantScopedOrder.objects.all().select_related("widget")}
        assert orders["O1"].widget is not None
        assert orders["O1"].widget.name == "W1"
        # widget_2 belongs to a different tenant - TenantScopedOrder itself has no
        # Meta.tenant_field to filter the O2 row out entirely, but the JOIN to its widget must
        # still come back empty instead of leaking widget_2's data across tenants.
        assert orders["O2"].widget is None


@pytest.mark.asyncio
async def test_select_related_join_requires_a_tenant_scope_for_the_related_model(db):
    """TenantScopedOrder has no Meta.tenant_field of its own, so .all() alone never requires an
    active tenant scope - but joining to TenantScopedWidget (which DOES have one) must still
    demand it, exactly like Widget.objects.all() would on its own."""
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    await TenantScopedOrder.objects.create(name="O1", widget=widget)

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError):
        await TenantScopedOrder.objects.all().select_related("widget")


@pytest.mark.asyncio
async def test_select_related_join_excludes_a_soft_deleted_related_row(db):
    """factory is soft-deleted BEFORE widget is created pointing at it - soft-deleting something
    doesn't prevent a later insert from pointing a new FK at it, so this is a realistic way for a
    live row to end up referencing an already-soft-deleted related row."""
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="F1", company_id=1)
        await factory.delete()
        widget = await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory)

        fetched = await TenantScopedWidget.objects.filter(pk=widget.pk).select_related("factory").first()
        assert fetched is not None
        assert fetched.factory is None


@pytest.mark.asyncio
async def test_select_related_nested_chain_scopes_each_hop_independently(db):
    """order -> widget -> factory: the widget hop matches the active tenant, but the factory
    hop's OWN tenant doesn't - each hop must be scoped independently, not just the last one."""
    factory_other_tenant = await TenantScopedFactory.objects.create(name="F-other", company_id=2)
    widget = await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory_other_tenant)
    order = await TenantScopedOrder.objects.create(name="O1", widget=widget)

    with Tenancy.scope(1):
        fetched = await TenantScopedOrder.objects.filter(pk=order.pk).select_related("widget__factory").first()
        assert fetched.widget is not None
        assert fetched.widget.name == "W1"
        assert fetched.widget.factory is None


@pytest.mark.asyncio
async def test_only_scopes_join_to_the_active_tenant(db):
    """.only("relation__field") builds its JOIN through a different code path than
    select_related() (_get_only(), not _join_select_related()) - it needs the exact same
    Meta.tenant_field scoping applied independently, with or without a matching
    .select_related() call."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        orders = {o.name: o for o in await TenantScopedOrder.objects.all().only("id", "name", "widget__name")}
        assert orders["O1"].widget is not None
        assert orders["O1"].widget.name == "W1"
        assert orders["O2"].widget is None


@pytest.mark.asyncio
async def test_only_combined_with_select_related_does_not_let_the_unscoped_join_win(db):
    """select_related("widget") computes the correctly-scoped JOIN condition, but _get_only()'s
    own join for the SAME relation runs FIRST in _make_query() and claims the table in the
    table-identity dedup (_join_table()) - unless _get_only() ALSO applies the ambient scope
    condition itself, its unscoped join wins the dedup race and select_related()'s later, correctly
    scoped join for the same relation is silently skipped."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        orders = {
            o.name: o
            for o in await TenantScopedOrder.objects.all().select_related("widget").only("id", "name", "widget__name")
        }
        assert orders["O1"].widget is not None
        assert orders["O1"].widget.name == "W1"
        assert orders["O2"].widget is None


@pytest.mark.asyncio
async def test_only_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """_get_only()'s ambient-scope condition is resolved without threading value_wrapper_refs (see
    _query_is_plannable()'s own docstring) - if this query shape were cached, the FIRST
    tenant to build it would freeze into the cached JOIN forever, silently leaking across every
    later tenant reusing the same shape. Two different tenants querying with the identical
    .only()-with-relation shape back to back must each see their own, correctly scoped data.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = {o.name: o for o in await TenantScopedOrder.objects.all().only("id", "name", "widget__name")}
    with Tenancy.scope(2):
        second = {o.name: o for o in await TenantScopedOrder.objects.all().only("id", "name", "widget__name")}

    assert len(StatementPlans.plans) > cache_size_before
    assert first["O1"].widget is not None and first["O1"].widget.name == "W1"
    assert first["O2"].widget is None
    assert second["O1"].widget is None
    assert second["O2"].widget is not None and second["O2"].widget.name == "W2"


@pytest.mark.asyncio
async def test_select_related_extra_condition_combines_with_the_ambient_tenant_filter(db):
    """Select(relation, extra_condition=...) and the ambient tenant filter must AND together -
    neither alone is enough to hydrate the relation."""
    widget_match = await TenantScopedWidget.objects.create(name="Match", company_id=1)
    widget_wrong_name = await TenantScopedWidget.objects.create(name="Other", company_id=1)
    widget_wrong_tenant = await TenantScopedWidget.objects.create(name="Match", company_id=2)
    order_match = await TenantScopedOrder.objects.create(name="O1", widget=widget_match)
    order_wrong_name = await TenantScopedOrder.objects.create(name="O2", widget=widget_wrong_name)
    order_wrong_tenant = await TenantScopedOrder.objects.create(name="O3", widget=widget_wrong_tenant)

    order_pks = [order_match.pk, order_wrong_name.pk, order_wrong_tenant.pk]
    with Tenancy.scope(1):
        orders = {
            o.name: o
            for o in await TenantScopedOrder.objects.filter(pk__in=order_pks)
            .select_related(Select("widget", extra_condition=Q(name="Match")))
            .all()
        }
        # extra_condition matches AND the ambient tenant filter matches
        assert orders["O1"].widget is not None
        assert orders["O1"].widget.name == "Match"
        # extra_condition fails (wrong name), ambient tenant filter would have passed
        assert orders["O2"].widget is None
        # extra_condition would match (same name), but the ambient tenant filter excludes it
        assert orders["O3"].widget is None


# ============================================================================
# Round-2 finding: select_related()/.only() were only two of at least five independently broken
# call sites sharing the SAME root cause - LookupPaths.get_joins_for_related_field()/
# get_nested_field() never applied Meta.tenant_field/Meta.soft_delete_field scoping to a JOIN.
# .filter(relation__field=...), .values("relation__field")/.group_by(...), .order_by(...) and
# F("relation__field") annotations each cross a relation through their own independent code path
# and each needed the same fix applied independently - see test_bug_hunt_round2.py for the
# non-JOIN-related findings from the same round.
# ============================================================================


@pytest.mark.asyncio
async def test_filter_across_relation_scopes_join_to_the_active_tenant(db):
    """.filter(relation__field=value) builds its JOIN through Q._get_nested_filter() - a code
    path independent of select_related()/.only(), needing the exact same Meta.tenant_field
    scoping applied on its own."""
    widget_1 = await TenantScopedWidget.objects.create(name="Shared", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="Shared", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        matches = await TenantScopedOrder.objects.filter(widget__name="Shared")
        assert {o.pk for o in matches} == {order_1.pk}


@pytest.mark.asyncio
async def test_values_across_relation_scopes_join_to_the_active_tenant(db):
    """.values("relation__field") builds its JOIN through _join_table_with_forwarded_fields()
    (shared with .group_by()) - needing the exact same Meta.tenant_field scoping applied on its
    own."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        rows = {
            row["name"]: row["widget__name"]
            for row in await TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).values(
                "name", "widget__name"
            )
        }
        assert rows["O1"] == "W1"
        # widget_2 belongs to a different tenant - the JOIN must come back empty, not leak
        # widget_2's name across tenants.
        assert rows["O2"] is None


@pytest.mark.asyncio
async def test_values_filter_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Regression test: QUERY_SHAPE_CACHE's ambient-scope exclusions (for a .filter()/.order_by()/
    .annotate() crossing a Meta.tenant_field/Meta.soft_delete_field-scoped relation) used to live
    ONLY on QuerySet's own _query_is_plannable() override - ValuesQuery/ValuesListQuery
    never go through it (they inherit straight from AwaitableQuery, a QuerySet sibling, not a
    QuerySet itself), so the exact same exclusion silently never applied the moment .values()/
    .values_list() was chained on - the FIRST tenant to build this shape via .values() froze its
    own company_id into the cached JOIN, and every later tenant's .values() call reused it. A
    plain (non-.values()) .filter() on the same shape was already covered by
    test_filter_touching_a_tenant_scoped_relation_is_excluded_from_the_query_shape_cache.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = await TenantScopedOrder.objects.filter(widget__name="W1").values("name")
    with Tenancy.scope(2):
        second = await TenantScopedOrder.objects.filter(widget__name="W1").values("name")

    assert len(StatementPlans.plans) > cache_size_before
    assert {row["name"] for row in first} == {"O1"}
    assert {row["name"] for row in second} == set()


@pytest.mark.asyncio
async def test_values_order_by_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Same ValuesQuery/ValuesListQuery gap as the .filter() case above, for .order_by() crossing
    a tenant-scoped relation.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        await TenantScopedOrder.objects.all().order_by("widget__name").values("name")
    with Tenancy.scope(2):
        await TenantScopedOrder.objects.all().order_by("widget__name").values("name")

    assert len(StatementPlans.plans) > cache_size_before


@pytest.mark.asyncio
async def test_values_annotate_f_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Same ValuesQuery/ValuesListQuery gap as the .filter()/.order_by() cases above, for a bare
    F("relation__field") annotation crossing a tenant-scoped relation.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    StatementPlans.plans.clear()
    cache_size_before = 0

    def annotated_query():
        return TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).annotate(
            widget_name=F("widget__name")
        )

    with Tenancy.scope(1):
        first = {row["name"]: row["widget_name"] for row in await annotated_query().values("name", "widget_name")}
    with Tenancy.scope(2):
        second = {row["name"]: row["widget_name"] for row in await annotated_query().values("name", "widget_name")}

    assert len(StatementPlans.plans) > cache_size_before
    assert first["O1"] == "W1"
    assert first["O2"] is None
    assert second["O1"] is None
    assert second["O2"] == "W2"


@pytest.mark.asyncio
async def test_values_group_by_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Same ValuesQuery/ValuesListQuery gap as the .filter()/.order_by()/.annotate() cases above,
    for .group_by() crossing a tenant-scoped relation - _query_is_plannable() used to check
    neither self._group_bys nor the selected field list at all, only filter/order_by/annotate.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        await TenantScopedOrder.objects.all().group_by("widget__name").values("widget__name")
    with Tenancy.scope(2):
        await TenantScopedOrder.objects.all().group_by("widget__name").values("widget__name")

    assert len(StatementPlans.plans) > cache_size_before


@pytest.mark.asyncio
async def test_values_list_bare_forwarded_field_touching_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(
    db,
):
    """The narrowest reproduction of the same gap - no .filter()/.order_by()/.annotate()/
    .group_by() at all, just selecting a forwarded field ("widget__name") is enough to reach
    add_field_to_select_query()'s shared _join_table_with_forwarded_fields() helper and fold the
    ambient tenant condition into an unthreaded JOIN.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    StatementPlans.plans.clear()
    cache_size_before = 0

    def selecting_query():
        return TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).order_by("pk")

    with Tenancy.scope(1):
        first = await selecting_query().values_list("widget__name", flat=True)
    with Tenancy.scope(2):
        second = await selecting_query().values_list("widget__name", flat=True)

    assert len(StatementPlans.plans) > cache_size_before
    assert list(first) == ["W1", None]
    assert list(second) == [None, "W2"]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_list_distinct_on_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(
    db,
):
    """FieldSelectQuery._query_is_plannable() never checked self._distinct_on at all - its
    own docstring incorrectly claimed ValuesQuery/ValuesListQuery don't support DISTINCT ON, but
    they do (distinct_on= is forwarded from QuerySet.distinct("...")) and reach the same
    get_distinct() DISTINCT ON branch that folds an unthreaded ambient-scope condition into the
    JOIN. Selects a field that does NOT itself cross the relation ("id", not "widget__name") and
    gives no explicit .order_by() (get_distinct()'s own "ORDER BY must start with DISTINCT ON
    fields" validation only fires when there IS an ordering to compare against - an empty one
    skips it entirely) - isolates the distinct_on-specific gap from the already-fixed ordering/
    selected-field-list checks right above, which would otherwise also exclude this shape for an
    unrelated reason and mask a regression here.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)

    StatementPlans.plans.clear()
    cache_size_before = 0

    def selecting_query():
        return TenantScopedOrder.objects.filter(pk=order_1.pk).distinct("widget__name")

    with Tenancy.scope(1):
        await selecting_query().values_list("id", flat=True)
    with Tenancy.scope(2):
        await selecting_query().values_list("id", flat=True)

    assert len(StatementPlans.plans) > cache_size_before


@pytest.mark.asyncio
async def test_annotate_f_across_relation_scopes_join_to_the_active_tenant(db):
    """F("relation__field") builds its JOIN through LookupPaths.get_nested_field() - needing
    the exact same Meta.tenant_field scoping applied on its own."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        rows = {
            row["name"]: row["widget_name"]
            for row in await TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk])
            .annotate(widget_name=F("widget__name"))
            .values("name", "widget_name")
        }
        assert rows["O1"] == "W1"
        assert rows["O2"] is None


@pytest.mark.asyncio
async def test_order_by_across_relation_scopes_join_to_the_active_tenant(db):
    """.order_by("relation__field") builds its JOIN through get_ordering() - needing the exact
    same Meta.tenant_field scoping applied on its own, or the generated JOIN's ON clause would
    never restrict the related row to the active tenant at all."""
    with Tenancy.scope(1):
        sql = TenantScopedOrder.objects.all().order_by("widget__name").sql(params_inline=True)
    assert "company_id" in sql


@pytest.mark.asyncio
async def test_filter_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Q._get_nested_filter()'s ambient-scope condition is resolved without threading
    value_wrapper_refs, exactly like _get_only()'s own (see
    test_only_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant) - if this
    query shape were cached, the FIRST tenant to build it would freeze into the cached JOIN
    forever.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = await TenantScopedOrder.objects.filter(widget__name="W1")
    with Tenancy.scope(2):
        second = await TenantScopedOrder.objects.filter(widget__name="W1")

    assert len(StatementPlans.plans) > cache_size_before
    assert {o.name for o in first} == {"O1"}
    assert {o.name for o in second} == set()


@pytest.mark.asyncio
async def test_order_by_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """get_ordering()'s ambient-scope condition is resolved without threading value_wrapper_refs -
    same reasoning as the .filter() cache-exclusion test above.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        await TenantScopedOrder.objects.all().order_by("widget__name")
    with Tenancy.scope(2):
        await TenantScopedOrder.objects.all().order_by("widget__name")

    assert len(StatementPlans.plans) > cache_size_before


@pytest.mark.asyncio
async def test_annotate_f_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """LookupPaths.get_nested_field()'s ambient-scope condition is resolved without threading
    value_wrapper_refs - same reasoning as the .filter()/.order_by() cache-exclusion tests
    above.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        await TenantScopedOrder.objects.filter(pk=order_1.pk).annotate(widget_name=F("widget__name"))
    with Tenancy.scope(2):
        await TenantScopedOrder.objects.filter(pk=order_1.pk).annotate(widget_name=F("widget__name"))

    assert len(StatementPlans.plans) > cache_size_before


@pytest.mark.asyncio
async def test_annotate_window_across_relation_scopes_join_to_the_active_tenant(db):
    """Window(Sum("relation__field"), ...) resolves its own field through the exact same
    F(...).get_result() -> LookupPaths.get_nested_field() path a bare F("relation__field")
    annotation does (see Window.get_result()/FieldWindowFunction.build()) - needing the identical
    Meta.tenant_field scoping applied on its own, or the JOIN would pull in a related row
    belonging to ANY tenant regardless of the current scope."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        rows = {
            row["name"]: row["widget_company"]
            for row in await TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk])
            .annotate(widget_company=Window(WindowSum("widget__company_id"), partition_by=["id"]))
            .values("name", "widget_company")
        }
        assert rows["O1"] == 1
        assert rows["O2"] is None


@pytest.mark.asyncio
async def test_annotate_window_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """Window's own field/partition_by/order_by resolution shares F("relation__field")'s
    ambient-scope-folding JOIN, unthreaded through value_wrapper_refs - same reasoning as the
    other annotate cache-exclusion tests above. Regression test: without this exclusion, the
    FIRST tenant to build this query shape freezes its own company_id into the cached JOIN
    forever, and every later tenant silently reuses it - a real cross-tenant data leak.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    StatementPlans.plans.clear()
    cache_size_before = 0

    def annotated_query():
        return TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).annotate(
            widget_company=Window(WindowSum("widget__company_id"), partition_by=["id"])
        )

    with Tenancy.scope(1):
        first = {
            row["name"]: row["widget_company"] for row in await annotated_query().values("name", "widget_company")
        }
    with Tenancy.scope(2):
        second = {
            row["name"]: row["widget_company"] for row in await annotated_query().values("name", "widget_company")
        }

    assert len(StatementPlans.plans) > cache_size_before
    assert first["O1"] == 1
    assert first["O2"] is None
    assert second["O1"] is None
    assert second["O2"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window",
    [
        lambda: Window(WindowSum(F("widget__company_id") + 0), partition_by=["id"]),
        lambda: Window(NthValue("widget__company_id"), partition_by=["id"]),
        lambda: Window(NthValue(F("widget__company_id") + 0), partition_by=["id"]),
    ],
    ids=["expression field", "nth value", "nth value of an expression"],
)
async def test_a_window_reading_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db, window):
    """A window function reading a relation with a default scope through an expression or
    NthValue() joins it with the active tenant's condition - a plan would keep the first tenant's.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    def companies() -> object:
        return (
            TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk])
            .annotate(widget_company=window())
            .values("name", "widget_company")
        )

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = {row["name"]: row["widget_company"] for row in await companies()}
    with Tenancy.scope(2):
        second = {row["name"]: row["widget_company"] for row in await companies()}

    assert len(StatementPlans.plans) > cache_size_before
    assert first == {"O1": 1, "O2": None}
    assert second == {"O1": None, "O2": 2}


@pytest.mark.asyncio
async def test_a_window_condition_reading_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """A window aggregate's _filter= condition crossing a relation with a default scope joins it
    with the active tenant's condition - a plan would keep the first tenant's.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    def counts() -> object:
        return (
            TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk])
            .annotate(matching=Window(AggregateCount("id", _filter=Q(widget__company_id__gte=1)), partition_by=["id"]))
            .values("name", "matching")
        )

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = {row["name"]: row["matching"] for row in await counts()}
    with Tenancy.scope(2):
        second = {row["name"]: row["matching"] for row in await counts()}

    assert len(StatementPlans.plans) > cache_size_before
    assert first == {"O1": 1, "O2": 0}
    assert second == {"O1": 0, "O2": 1}


@pytest.mark.asyncio
async def test_annotate_bare_string_function_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(
    db,
):
    """A bare string field (Sum("relation__field")/Count(...)/Coalesce(...)/... - the ordinary,
    most common way to call these, not F("relation__field")) resolves through the exact same
    LookupPaths.get_nested_field() ambient-scope-folding JOIN as F() itself, but
    _annotation_touches_ambient_scoped_relation()'s AnnotationFunction branch only ever checked
    `annotation.field` when it was an Expression - a plain string fell straight through
    unnoticed, identical in shape to the already-fixed Window case just below (which checks its
    own field this same way) but missed on this, the far more common sibling path. Regression
    test: without this exclusion, the FIRST tenant to build this query shape freezes its own
    company_id into the cached JOIN forever, and every later tenant silently reuses it - a real
    cross-tenant data leak.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""
    from hare.query.functions import Sum

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    StatementPlans.plans.clear()
    cache_size_before = 0

    def annotated_query():
        return TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).annotate(
            widget_company=Sum("widget__company_id")
        )

    with Tenancy.scope(1):
        first = {
            row["name"]: row["widget_company"] for row in await annotated_query().values("name", "widget_company")
        }
    with Tenancy.scope(2):
        second = {
            row["name"]: row["widget_company"] for row in await annotated_query().values("name", "widget_company")
        }

    assert len(StatementPlans.plans) > cache_size_before
    assert first["O1"] == 1
    assert first["O2"] is None
    assert second["O1"] is None
    assert second["O2"] == 2


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_across_relation_scopes_join_to_the_active_tenant(db):
    """.distinct("relation__field") builds its JOIN through get_distinct()'s own DISTINCT ON
    branch - a code path independent of select_related()/.only()/.filter()/.order_by()/F(),
    needing the exact same Meta.tenant_field scoping applied on its own. Without it, two orders
    whose widgets share the SAME name but belong to DIFFERENT tenants collapse into ONE DISTINCT
    ON group instead of two, since the unscoped JOIN lets both resolve to name="Shared"."""
    widget_1 = await TenantScopedWidget.objects.create(name="Shared", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="Shared", company_id=2)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    order_2 = await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        distinct_rows = await TenantScopedOrder.objects.filter(pk__in=[order_1.pk, order_2.pk]).distinct(
            "widget__name"
        )
        assert len(distinct_rows) == 2


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_touching_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """get_distinct()'s ambient-scope condition is resolved without threading value_wrapper_refs -
    same reasoning as the .filter()/.order_by()/F() cache-exclusion tests above.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""

    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    order_1 = await TenantScopedOrder.objects.create(name="O1", widget=widget_1)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        await TenantScopedOrder.objects.filter(pk=order_1.pk).distinct("widget__name")
        first = (
            await TenantScopedOrder.objects.filter(pk=order_1.pk)
            .distinct("widget__name")
            .values_list("widget__name", flat=True)
        )
    with Tenancy.scope(2):
        await TenantScopedOrder.objects.filter(pk=order_1.pk).distinct("widget__name")
        second = (
            await TenantScopedOrder.objects.filter(pk=order_1.pk)
            .distinct("widget__name")
            .values_list("widget__name", flat=True)
        )

    assert len(StatementPlans.plans) > cache_size_before
    assert first == ["W1"]
    assert second == [None]


# ============================================================================
# A queryset's own BASE ambient tenant filter is frozen at CONSTRUCTION time (Manager.
# get_queryset() reads Tenancy.current.get() once, right there) - but a JOIN's own ambient scope
# condition (select_related()/a nested `related__field=value` filter) used to be resolved lazily,
# inside _make_query(), which only runs when the query is actually awaited. Building a queryset
# under one Tenancy.scope() and awaiting it later - after that block exited, or under a DIFFERENT
# scope, an ordinary "build now, execute slightly later" pattern - used to make the base filter
# and the JOIN condition silently disagree, or raise ConfigurationError purely because of WHEN the
# query happened to be awaited. QuerySet._ambient_tenant_snapshot now freezes the tenant value at
# construction time too, threaded through ExpressionContext.ambient_tenant_snapshot to every JOIN
# this query builds, however lazily.
# ============================================================================


@pytest.mark.asyncio
async def test_select_related_join_is_scoped_to_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="Acme", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory_id=factory.pk)
    queryset = TenantScopedWidget.objects.filter(name="W1").select_related("factory")

    with Tenancy.scope(1):
        widgets = list(await queryset)
    assert len(widgets) == 1
    assert widgets[0].factory is not None
    assert widgets[0].factory.pk == factory.pk

    with Tenancy.scope(2):
        assert await queryset == []


@pytest.mark.asyncio
async def test_select_related_query_awaited_without_a_tenant_raises(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="Acme", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory_id=factory.pk)
        queryset = TenantScopedWidget.objects.filter(name="W1").select_related("factory")

    assert Tenancy.current.get() is None
    with pytest.raises(QueryError, match="no tenant is active"):
        await queryset


@pytest.mark.asyncio
async def test_nested_filter_across_a_tenant_scoped_relation_is_scoped_to_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="Acme", company_id=1)
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory_id=factory.pk)
    queryset = TenantScopedWidget.objects.filter(factory__name="Acme")

    with Tenancy.scope(1):
        widgets = list(await queryset)
    assert [widget.name for widget in widgets] == ["W1"]
    with Tenancy.scope(2):
        assert await queryset == []


@pytest.mark.asyncio
async def test_a_plan_across_a_tenant_scoped_relation_binds_each_tenant(db):
    """The plan recorded under one tenant binds the next tenant's scope on its JOIN."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)
    StatementPlans.plans.clear()

    results = {}
    for tenant in (1, 2, 1):
        with Tenancy.scope(tenant):
            orders = await TenantScopedOrder.objects.select_related("widget").order_by("name")
        results[tenant] = {order.name: order.widget.name if order.widget else None for order in orders}
        assert results[tenant] == {"O1": "W1" if tenant == 1 else None, "O2": "W2" if tenant == 2 else None}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_all_tenants_and_the_active_tenant_keep_apart_plans_across_a_relation(db):
    """all_tenants() drops the JOIN's tenant condition - a query with the default visibility of the
    same shape must not run on that plan."""
    widget_1 = await TenantScopedWidget.objects.create(name="W1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="W2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        everything = await TenantScopedOrder.objects.all_tenants().select_related("widget").order_by("name")
        scoped = await TenantScopedOrder.objects.select_related("widget").order_by("name")

    assert {order.name: order.widget.name if order.widget else None for order in everything} == {
        "O1": "W1",
        "O2": "W2",
    }
    assert {order.name: order.widget.name if order.widget else None for order in scoped} == {"O1": "W1", "O2": None}
