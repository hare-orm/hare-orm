import asyncio

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries, requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.sqlite.exceptions import SqliteTriggerRecursionLimitError
from hare.exceptions import ConfigurationError, IntegrityError, ProtectedError
from hare.fields import PROTECT
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.instance_deletion import InstanceDeletion
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    DoubleFK,
    ProtectedChild,
    ProtectedChildByCode,
    ProtectedParent,
    ProtectedParentWithCode,
    TransitiveProtectCycleAlpha,
    TransitiveProtectCycleBeta,
    TransitiveProtectCycleGuard,
    TransitiveProtectGuard,
    TransitiveProtectLeaf,
    TransitiveProtectLeafGuard,
    TransitiveProtectLooseGuard,
    TransitiveProtectLooseMiddle,
    TransitiveProtectLooseNote,
    TransitiveProtectLooseRoot,
    TransitiveProtectMiddle,
    TransitiveProtectNote,
    TransitiveProtectRoot,
    TransitiveProtectSelfReferential,
    TransitiveProtectTenantGuard,
    TransitiveProtectTenantMiddle,
)


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real file-backed SQLite DB, not `:memory:` - Transactions.autonomous() opens a genuinely
    separate connection, and a fresh connection to `:memory:` is a completely separate, empty
    database (mirrors the same fixture in test_autonomous_transaction.py)."""
    db_path = tmp_path / "protect_autonomous_test.sqlite"
    async with hare_test_context(
        ["tests.testmodels"], db_url=f"sqlite+aiosqlite:///{db_path}?synchronous=OFF", connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def parent_with_child(db):
    parent = await ProtectedParent.objects.create(name="Parent")
    child = await ProtectedChild.objects.create(name="Child", parent=parent)
    return parent, child


# ============================================================================
# Single-instance Model.delete()
# ============================================================================


@pytest.mark.asyncio
async def test_delete_protected_parent_raises(db, parent_with_child):
    parent, child = parent_with_child

    with pytest.raises(ProtectedError):
        await parent.delete()

    # nothing was actually deleted
    assert await ProtectedParent.objects.filter(pk=parent.pk).exists()
    assert await ProtectedChild.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_delete_protected_error_carries_protecting_objects(db, parent_with_child):
    parent, child = parent_with_child

    with pytest.raises(ProtectedError) as exc_info:
        await parent.delete()

    assert exc_info.value.protected_objects == [child]


@pytest.mark.asyncio
async def test_delete_parent_succeeds_once_child_removed(db, parent_with_child):
    parent, child = parent_with_child
    await child.delete()

    await parent.delete()
    assert not await ProtectedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_delete_parent_with_no_children_succeeds(db):
    parent = await ProtectedParent.objects.create(name="Lonely")
    await parent.delete()
    assert not await ProtectedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_delete_child_itself_is_unaffected_by_protect(db, parent_with_child):
    """PROTECT guards the PARENT from deletion - deleting the child (the protecting side) is
    always fine."""
    parent, child = parent_with_child
    await child.delete()
    assert not await ProtectedChild.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_unrelated_model_delete_unaffected(db):
    """A model with no PROTECT relations at all must not pay any cascade-check cost/behavior
    change."""
    root = await DoubleFK.objects.create(name="root")
    await root.delete()
    assert not await DoubleFK.objects.filter(pk=root.pk).exists()


# ============================================================================
# Bulk QuerySet.delete()
# ============================================================================


@pytest.mark.asyncio
async def test_bulk_delete_protected_raises(db, parent_with_child):
    parent, child = parent_with_child

    with pytest.raises(ProtectedError):
        await ProtectedParent.objects.filter(pk=parent.pk).delete()

    assert await ProtectedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_protected_respects_to_field(db):
    """The FK's to_field (here "code", not the PK "id") is what the shadow column on the child
    actually stores - the bulk PROTECT check must compare against THAT value, not the parent's
    raw pk, or it silently checks the wrong column (a real integrity-bypass risk if the two
    happen to overlap by coincidence, not just a crash when they don't)."""
    parent = await ProtectedParentWithCode.objects.create(code=555)
    await ProtectedChildByCode.objects.create(name="Child", parent=parent)

    with pytest.raises(ProtectedError):
        await ProtectedParentWithCode.objects.filter(pk=parent.pk).delete()

    assert await ProtectedParentWithCode.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_mixed_protected_and_unprotected(db, parent_with_child):
    """One matched row is protected, another isn't - the whole bulk delete must be rejected
    (partial bulk deletes would be a worse surprise than an all-or-nothing failure)."""
    protected_parent, child = parent_with_child
    free_parent = await ProtectedParent.objects.create(name="Free")

    with pytest.raises(ProtectedError):
        await ProtectedParent.objects.all().delete()

    assert await ProtectedParent.objects.filter(pk=protected_parent.pk).exists()
    assert await ProtectedParent.objects.filter(pk=free_parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_no_matches_no_protected_error(db, parent_with_child):
    """The filter matches nothing, even though a PROTECT relation exists on the model - no rows,
    no error."""
    result = await ProtectedParent.objects.filter(name="Does Not Exist").delete()
    assert result == 0


@pytest.mark.asyncio
async def test_bulk_delete_unprotected_rows_succeeds(db):
    parent = await ProtectedParent.objects.create(name="Free")
    result = await ProtectedParent.objects.filter(pk=parent.pk).delete()
    assert result == 1
    assert not await ProtectedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_unrelated_model_unaffected(db):
    await DoubleFK.objects.create(name="a")
    await DoubleFK.objects.create(name="b")
    result = await DoubleFK.objects.all().delete()
    assert result == 2


# ============================================================================
# Field validation / OnDelete enum
# ============================================================================


@pytest.mark.asyncio
async def test_protect_requires_no_null(db):
    """PROTECT doesn't need null=True (unlike SET_NULL) - the row is never actually nulled."""
    assert ProtectedChild._meta.fields_map["parent"].on_delete == PROTECT  # type: ignore[attr-defined]


def test_on_delete_rejects_unknown_value():
    with pytest.raises(ConfigurationError):
        from hare import fields
        from hare.models import Model

        class BadModel(Model):
            other: fields.ForeignKeyRelation[ProtectedParent] = fields.ForeignKeyField(
                "models.ProtectedParent",
                on_delete="NOT_A_REAL_ACTION",  # type: ignore[arg-type]
            )


# ============================================================================
# Transactions.autonomous()
# ============================================================================


@pytest.mark.asyncio
async def test_protect_raised_inside_autonomous_does_not_lose_earlier_writes(file_db):
    """A ProtectedError raised partway through an autonomous() block doesn't undo whatever that
    same block already wrote before hitting it - autonomous() commits progressively, it isn't a
    single all-or-nothing transaction the way the enclosing scope's own atomic() would be."""
    parent = await ProtectedParent.objects.create(name="P")
    await ProtectedChild.objects.create(name="C", parent=parent)

    with pytest.raises(ProtectedError):
        async with Transactions.autonomous() as conn:
            await ProtectedParent.objects.filter(pk=parent.pk).using(conn).update(name="renamed first")
            await parent.delete(using=conn)

    refreshed = await ProtectedParent.objects.get(pk=parent.pk)
    assert refreshed.name == "renamed first"


# ============================================================================
# Transitive PROTECT - a hard delete that reaches a PROTECT-guarded row through CASCADE
# ============================================================================


@pytest_asyncio.fixture
async def root_with_protected_middle(db):
    root = await TransitiveProtectRoot.objects.create(name="root")
    middle = await TransitiveProtectMiddle.objects.create(name="middle", root=root)
    guard = await TransitiveProtectGuard.objects.create(name="guard", middle=middle)
    return root, middle, guard


@pytest_asyncio.fixture
async def root_with_protected_leaf(db):
    root = await TransitiveProtectRoot.objects.create(name="root")
    middle = await TransitiveProtectMiddle.objects.create(name="middle", root=root)
    leaf = await TransitiveProtectLeaf.objects.create(name="leaf", middle=middle)
    guard = await TransitiveProtectLeafGuard.objects.create(name="guard", leaf=leaf)
    return root, middle, leaf, guard


@pytest.mark.asyncio
async def test_delete_raises_protected_error_for_protect_one_cascade_hop_down(root_with_protected_middle):
    root, middle, guard = root_with_protected_middle

    with pytest.raises(ProtectedError) as exc_info:
        await root.delete()

    assert exc_info.value.protected_objects == [guard]
    assert "TransitiveProtectMiddle" in str(exc_info.value)
    assert "TransitiveProtectGuard.middle" in str(exc_info.value)
    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()
    assert await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()
    assert await TransitiveProtectGuard.objects.filter(pk=guard.pk).exists()


@pytest.mark.asyncio
async def test_delete_raises_protected_error_for_protect_two_cascade_hops_down(root_with_protected_leaf):
    root, middle, leaf, guard = root_with_protected_leaf

    with pytest.raises(ProtectedError) as exc_info:
        await root.delete()

    assert exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()
    assert await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()
    assert await TransitiveProtectLeaf.objects.filter(pk=leaf.pk).exists()
    assert await TransitiveProtectLeafGuard.objects.filter(pk=guard.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_raises_protected_error_for_protect_one_cascade_hop_down(root_with_protected_middle):
    root, middle, guard = root_with_protected_middle

    with pytest.raises(ProtectedError) as exc_info:
        await TransitiveProtectRoot.objects.filter(pk=root.pk).delete()

    assert exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()
    assert await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()
    assert await TransitiveProtectGuard.objects.filter(pk=guard.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_raises_protected_error_for_protect_two_cascade_hops_down(root_with_protected_leaf):
    root, middle, leaf, guard = root_with_protected_leaf

    with pytest.raises(ProtectedError) as exc_info:
        await TransitiveProtectRoot.objects.all().delete()

    assert exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()
    assert await TransitiveProtectLeaf.objects.filter(pk=leaf.pk).exists()
    assert await TransitiveProtectLeafGuard.objects.filter(pk=guard.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_with_one_protected_root_deletes_nothing(root_with_protected_middle):
    """Same all-or-nothing rule as a direct PROTECT: one blocked row among several matched ones
    rejects the whole bulk delete."""
    protected_root, __, guard = root_with_protected_middle
    free_root = await TransitiveProtectRoot.objects.create(name="free")
    free_middle = await TransitiveProtectMiddle.objects.create(name="free-middle", root=free_root)

    with pytest.raises(ProtectedError) as exc_info:
        await TransitiveProtectRoot.objects.all().delete()

    assert exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectRoot.objects.filter(pk=protected_root.pk).exists()
    assert await TransitiveProtectRoot.objects.filter(pk=free_root.pk).exists()
    assert await TransitiveProtectMiddle.objects.filter(pk=free_middle.pk).exists()


@pytest.mark.asyncio
async def test_delete_succeeds_once_transitive_guard_removed(root_with_protected_leaf):
    root, middle, leaf, guard = root_with_protected_leaf
    await guard.delete()

    await root.delete()

    assert not await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()
    assert not await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()
    assert not await TransitiveProtectLeaf.objects.filter(pk=leaf.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_succeeds_once_transitive_guard_removed(root_with_protected_leaf):
    root, middle, leaf, guard = root_with_protected_leaf
    await guard.delete()

    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).delete() == 1

    assert not await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()
    assert not await TransitiveProtectLeaf.objects.filter(pk=leaf.pk).exists()


@pytest.mark.asyncio
async def test_delete_ignores_cascade_branches_without_any_protect(db):
    root = await TransitiveProtectRoot.objects.create(name="root")
    note = await TransitiveProtectNote.objects.create(name="note", root=root)
    middle = await TransitiveProtectMiddle.objects.create(name="middle", root=root)

    await root.delete()

    assert not await TransitiveProtectNote.objects.filter(pk=note.pk).exists()
    assert not await TransitiveProtectMiddle.objects.filter(pk=middle.pk).exists()


@pytest.mark.asyncio
async def test_transitive_protect_is_found_through_a_sibling_branch_without_protect(db):
    """The walk skips the CASCADE branch with no PROTECT under it, but still finds the one that has
    one."""
    root = await TransitiveProtectRoot.objects.create(name="root")
    await TransitiveProtectNote.objects.create(name="note", root=root)
    middle = await TransitiveProtectMiddle.objects.create(name="middle", root=root)
    guard = await TransitiveProtectGuard.objects.create(name="guard", middle=middle)

    with pytest.raises(ProtectedError) as exc_info:
        await root.delete()

    assert exc_info.value.protected_objects == [guard]


@pytest.mark.asyncio
async def test_delete_root_without_children_unaffected_by_transitive_protect(db):
    root = await TransitiveProtectRoot.objects.create(name="lonely")
    await root.delete()
    assert not await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()


@pytest.mark.asyncio
async def test_transitive_protect_flag_only_set_for_models_with_a_protect_below_a_cascade(db):
    assert DeletionGraph.has_transitive_protect(TransitiveProtectRoot)
    assert DeletionGraph.has_transitive_protect(TransitiveProtectLooseRoot)
    # a direct PROTECT alone (check_protected's job) or a PROTECT that isn't below any CASCADE hop
    # doesn't count
    assert DeletionGraph.has_protecting_relations(ProtectedParent)
    assert not DeletionGraph.has_transitive_protect(ProtectedParent)
    assert not DeletionGraph.has_transitive_protect(TransitiveProtectGuard)
    assert not DeletionGraph.has_transitive_protect(TransitiveProtectLeafGuard)
    assert not DeletionGraph.has_transitive_protect(DoubleFK)


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_delete_without_protect_below_cascade_costs_no_extra_queries(db):
    root = await DoubleFK.objects.create(name="root")

    async with capture_queries() as counter:
        await root.delete()

    assert counter.count == 1
    assert counter.queries[0].upper().startswith("DELETE")


@pytest.mark.asyncio
async def test_delete_finds_transitive_protect_on_tenant_scoped_model_whatever_the_active_tenant(db):
    root = await TransitiveProtectRoot.objects.create(name="root")
    with Tenancy.scope(1):
        middle = await TransitiveProtectTenantMiddle.objects.create(name="tenant-middle", company_id=1, root=root)
    guard = await TransitiveProtectTenantGuard.objects.create(name="guard", middle=middle)

    with Tenancy.scope(2), pytest.raises(ProtectedError) as scoped_exc_info:
        await root.delete()
    with pytest.raises(ProtectedError) as unscoped_exc_info:
        await TransitiveProtectRoot.objects.filter(pk=root.pk).delete()

    assert scoped_exc_info.value.protected_objects == [guard]
    assert unscoped_exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectRoot.objects.filter(pk=root.pk).exists()


@pytest.mark.asyncio
async def test_delete_through_python_side_cascade_walk_raises_protected_error(db):
    """A model with a db_constraint=False CASCADE child hard-deletes through the Python-side
    cascade (Model.delete()) / row-by-row (QuerySet.delete()) path instead of one DELETE."""
    root = await TransitiveProtectLooseRoot.objects.create(name="root")
    note = await TransitiveProtectLooseNote.objects.create(name="note", root=root)
    middle = await TransitiveProtectLooseMiddle.objects.create(name="middle", root=root)
    guard = await TransitiveProtectLooseGuard.objects.create(name="guard", middle=middle)

    with pytest.raises(ProtectedError) as instance_exc_info:
        await root.delete()
    with pytest.raises(ProtectedError) as bulk_exc_info:
        await TransitiveProtectLooseRoot.objects.filter(pk=root.pk).delete()

    assert instance_exc_info.value.protected_objects == [guard]
    assert bulk_exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectLooseRoot.objects.filter(pk=root.pk).exists()
    assert await TransitiveProtectLooseNote.objects.filter(pk=note.pk).exists()
    assert await TransitiveProtectLooseMiddle.objects.filter(pk=middle.pk).exists()


@pytest.mark.asyncio
async def test_transitive_protect_on_self_referential_cascade(db):
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    child = await TransitiveProtectSelfReferential.objects.create(name="child", parent=root)
    grandchild = await TransitiveProtectSelfReferential.objects.create(name="grandchild", parent=child)
    outside_guard = await TransitiveProtectSelfReferential.objects.create(name="outside", guardian=grandchild)

    with pytest.raises(ProtectedError) as instance_exc_info:
        await root.delete()
    with pytest.raises(ProtectedError) as bulk_exc_info:
        await TransitiveProtectSelfReferential.objects.filter(pk=root.pk).delete()

    assert instance_exc_info.value.protected_objects == [outside_guard]
    assert bulk_exc_info.value.protected_objects == [outside_guard]
    assert (
        await TransitiveProtectSelfReferential.objects.filter(pk__in=[root.pk, child.pk, grandchild.pk]).count() == 3
    )


@pytest.mark.asyncio
async def test_transitive_protect_ignores_protector_inside_the_same_cascade_tree(db):
    """A guard that the same cascade removes too doesn't block - only one from outside the tree
    does, and only that one shows up in protected_objects."""
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    protected_child = await TransitiveProtectSelfReferential.objects.create(name="protected", parent=root)
    await TransitiveProtectSelfReferential.objects.create(name="inside-guard", parent=root, guardian=protected_child)

    await DeletionCollector.check_protected_transitively(TransitiveProtectSelfReferential, [root.pk], None)

    outside_guard = await TransitiveProtectSelfReferential.objects.create(
        name="outside-guard", guardian=protected_child
    )
    with pytest.raises(ProtectedError) as exc_info:
        await DeletionCollector.check_protected_transitively(TransitiveProtectSelfReferential, [root.pk], None)

    assert exc_info.value.protected_objects == [outside_guard]


@pytest.mark.parametrize("guard_is_descendant_of_protected", [False, True])
@pytest.mark.parametrize("via_queryset", [False, True])
@pytest.mark.asyncio
async def test_delete_succeeds_when_the_protector_is_inside_the_same_cascade_tree(
    db, via_queryset, guard_is_descendant_of_protected
):
    """Bug: PROTECT was emitted in DDL as RESTRICT, which the database checks per row,
    immediately - the database-level CASCADE removing both the guard and the row it protects
    failed with a raw IntegrityError even though the Python PROTECT check (correctly) allowed the
    delete."""
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    protected_child = await TransitiveProtectSelfReferential.objects.create(name="protected", parent=root)
    guard_parent = protected_child if guard_is_descendant_of_protected else root
    inside_guard = await TransitiveProtectSelfReferential.objects.create(
        name="inside-guard", parent=guard_parent, guardian=protected_child
    )
    survivor = await TransitiveProtectSelfReferential.objects.create(name="survivor")

    if via_queryset:
        await TransitiveProtectSelfReferential.objects.filter(pk=root.pk).delete()
    else:
        await root.delete()

    remaining_pks = await TransitiveProtectSelfReferential.objects.all().values_list("pk", flat=True)
    assert remaining_pks == [survivor.pk]
    assert inside_guard.pk not in remaining_pks


@pytest.mark.asyncio
async def test_transitive_protect_walk_terminates_on_cyclic_data(db):
    """Two rows of a self-referential CASCADE model that are each other's parent - the walk has to
    stop at a row it already visited."""
    first = await TransitiveProtectSelfReferential.objects.create(name="first")
    second = await TransitiveProtectSelfReferential.objects.create(name="second", parent=first)
    first.parent = second
    await first.save()
    outside_guard = await TransitiveProtectSelfReferential.objects.create(name="outside", guardian=second)

    with pytest.raises(ProtectedError) as exc_info:
        await asyncio.wait_for(first.delete(), timeout=30)

    assert exc_info.value.protected_objects == [outside_guard]


@pytest.mark.asyncio
async def test_transitive_protect_walk_terminates_on_mutually_cascading_models(db):
    alpha = await TransitiveProtectCycleAlpha.objects.create(name="alpha")
    beta = await TransitiveProtectCycleBeta.objects.create(name="beta", alpha=alpha)
    alpha.beta = beta
    await alpha.save()
    guard = await TransitiveProtectCycleGuard.objects.create(name="guard", beta=beta)

    with pytest.raises(ProtectedError) as instance_exc_info:
        await asyncio.wait_for(alpha.delete(), timeout=30)
    with pytest.raises(ProtectedError) as bulk_exc_info:
        await asyncio.wait_for(TransitiveProtectCycleAlpha.objects.filter(pk=alpha.pk).delete(), timeout=30)

    assert instance_exc_info.value.protected_objects == [guard]
    assert bulk_exc_info.value.protected_objects == [guard]
    assert await TransitiveProtectCycleAlpha.objects.filter(pk=alpha.pk).exists()
    assert await TransitiveProtectCycleBeta.objects.filter(pk=beta.pk).exists()


@pytest.mark.asyncio
async def test_transitive_protect_with_composite_primary_key_root():
    async with hare_test_context(["tests.model_setup.models_transitive_protect_composite"]):
        from tests.model_setup.models_transitive_protect_composite import (
            CompositeProtectGuard,
            CompositeProtectMiddle,
            CompositeProtectRoot,
        )

        protected_root = await CompositeProtectRoot.objects.create(a=1, b=2, name="protected")
        free_root = await CompositeProtectRoot.objects.create(a=3, b=4, name="free")
        middle = await CompositeProtectMiddle.objects.create(name="middle", root=protected_root)
        guard = await CompositeProtectGuard.objects.create(name="guard", middle=middle)

        with pytest.raises(ProtectedError) as instance_exc_info:
            await protected_root.delete()
        with pytest.raises(ProtectedError) as bulk_exc_info:
            await CompositeProtectRoot.objects.filter(pk__in=[(1, 2), (3, 4)]).delete()

        assert instance_exc_info.value.protected_objects == [guard]
        assert bulk_exc_info.value.protected_objects == [guard]
        assert await CompositeProtectRoot.objects.filter(pk=(1, 2)).exists()
        assert await CompositeProtectRoot.objects.filter(pk=free_root.pk).exists()

        await guard.delete()
        assert await CompositeProtectRoot.objects.filter(pk__in=[(1, 2), (3, 4)]).delete() == 2
        assert not await CompositeProtectMiddle.objects.filter(pk=middle.pk).exists()


async def create_protected_tree(guard_is_descendant_of_protected: bool) -> TransitiveProtectSelfReferential:
    """Builds root -> protected, plus a guard PROTECTing `protected` from inside the same cascade
    tree - a sibling of `protected` or its own child. Returns the root."""
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    protected_child = await TransitiveProtectSelfReferential.objects.create(name="protected", parent=root)
    await TransitiveProtectSelfReferential.objects.create(
        name="inside-guard",
        parent=protected_child if guard_is_descendant_of_protected else root,
        guardian=protected_child,
    )
    return root


@requires_features(supports_transactions=True)
@pytest.mark.parametrize("guard_is_descendant_of_protected", [False, True])
@pytest.mark.parametrize("via_queryset", [False, True])
@pytest.mark.asyncio
async def test_delete_with_inside_protector_leaves_the_transaction_open_for_ddl(
    db, via_queryset, guard_is_descendant_of_protected
):
    """Bug: the PROTECT backstop was INITIALLY DEFERRED, so the delete's writes left pending trigger
    events on Postgres and a later ALTER TABLE/TRUNCATE of the table in the same transaction failed
    ("cannot ALTER TABLE ... because it has pending trigger events"). The deferral now only lasts
    for the delete itself."""
    table = TransitiveProtectSelfReferential._meta.db_table
    async with Transactions.atomic("models") as connection:
        root = await create_protected_tree(guard_is_descendant_of_protected)
        survivor = await TransitiveProtectSelfReferential.objects.create(name="survivor")

        if via_queryset:
            assert await TransitiveProtectSelfReferential.objects.filter(pk=root.pk).using(connection).delete() == 1
        else:
            await root.delete(using=connection)

        await connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "added_after_delete" INT')
        remaining_pks = (
            await TransitiveProtectSelfReferential.objects.all().using(connection).values_list("pk", flat=True)
        )
        assert remaining_pks == [survivor.pk]
        if connection.dialect.name == "postgresql":
            await connection.execute(f'TRUNCATE "{table}"')
        await connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "added_after_delete"')


@pytest.mark.parametrize("guard_is_descendant_of_protected", [False, True])
@pytest.mark.asyncio
async def test_bulk_delete_of_several_trees_with_inside_protectors(db, guard_is_descendant_of_protected):
    first_root = await create_protected_tree(guard_is_descendant_of_protected)
    second_root = await create_protected_tree(not guard_is_descendant_of_protected)

    deleted_count = await TransitiveProtectSelfReferential.objects.filter(
        pk__in=[first_root.pk, second_root.pk]
    ).delete()

    assert deleted_count == 2
    assert not await TransitiveProtectSelfReferential.objects.all().exists()


@pytest.mark.parametrize("guard_is_descendant_of_protected", [False, True])
@pytest.mark.asyncio
async def test_python_side_cascade_fallback_with_inside_protector(db, monkeypatch, guard_is_descendant_of_protected):
    """The row-by-row Python cascade (SQLite's fallback past the trigger recursion limit) deletes
    the tree in several statements - a guard deleted in a later statement than the row it protects
    must not fail the backstop in between."""
    root = await create_protected_tree(guard_is_descendant_of_protected)
    if root.get_connection().dialect.name != "sqlite":
        pytest.skip("Only SQLite falls back to a Python-side cascade")
    original_persist_hard_delete = InstanceDeletion.persist_hard_delete
    attempts: list[int] = []

    async def fail_first_native_delete(self, *args, **kwargs):
        if not attempts and self.pk == root.pk:
            attempts.append(self.pk)
            raise SqliteTriggerRecursionLimitError("too many levels of trigger recursion")
        return await original_persist_hard_delete(self, *args, **kwargs)

    monkeypatch.setattr(InstanceDeletion, "persist_hard_delete", fail_first_native_delete)
    await root.delete()

    assert attempts == [root.pk]
    assert not await TransitiveProtectSelfReferential.objects.all().exists()


@requires_features(supports_transactions=True)
@pytest.mark.parametrize("via_queryset", [False, True])
@pytest.mark.asyncio
async def test_outside_protector_still_blocks_through_the_database_backstop(db, monkeypatch, via_queryset):
    """The deferral only moves the check to the end of the delete: a guard outside the cascade
    tree still fails it - as ProtectedError from the Python check, and as IntegrityError from the
    database once that check is bypassed."""
    root = await TransitiveProtectSelfReferential.objects.create(name="root")
    protected_child = await TransitiveProtectSelfReferential.objects.create(name="protected", parent=root)
    await TransitiveProtectSelfReferential.objects.create(name="outside-guard", guardian=protected_child)

    with pytest.raises(ProtectedError):
        if via_queryset:
            await TransitiveProtectSelfReferential.objects.filter(pk=root.pk).delete()
        else:
            await root.delete()

    async def skip_check(*args, **kwargs):
        return None

    monkeypatch.setattr(DeletionCollector, "check_protected_transitively", skip_check)
    with pytest.raises(IntegrityError):
        async with Transactions.atomic("models") as connection:
            if via_queryset:
                await TransitiveProtectSelfReferential.objects.filter(pk=root.pk).using(connection).delete()
            else:
                await root.delete(using=connection)
    assert await TransitiveProtectSelfReferential.objects.filter(pk=protected_child.pk).exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_delete_keeps_the_timing_of_a_user_deferred_constraint(db):
    """Only the PROTECT backstops are deferred and then made immediate again - a user's own
    DEFERRABLE INITIALLY DEFERRED constraint on a cascade-deleted row stays deferred to commit."""
    table = TransitiveProtectSelfReferential._meta.db_table
    async with Transactions.atomic("models") as connection:
        await connection.execute(
            'CREATE TABLE "user_deferred_note" ("id" INTEGER PRIMARY KEY, "row_id" INTEGER '
            f'REFERENCES "{table}" ("id") DEFERRABLE INITIALLY DEFERRED)'
        )
        root = await create_protected_tree(guard_is_descendant_of_protected=True)
        await connection.execute(f'INSERT INTO "user_deferred_note" ("id", "row_id") VALUES (1, {root.pk})')

        await root.delete(using=connection)

        await connection.execute('DELETE FROM "user_deferred_note"')
    assert not await TransitiveProtectSelfReferential.objects.all().exists()


@pytest.mark.asyncio
async def test_a_bulk_delete_whose_cascade_removes_the_guard_of_another_root_succeeds(db):
    """A root's own PROTECT guard was checked without the cascade - a guard the same delete removes
    through another root's branch blocked the delete."""
    guarded_root = await TransitiveProtectSelfReferential.objects.create(name="guarded")
    other_root = await TransitiveProtectSelfReferential.objects.create(name="other")
    guard = await TransitiveProtectSelfReferential.objects.create(
        name="guard", parent=other_root, guardian=guarded_root
    )

    with pytest.raises(ProtectedError) as exc_info:
        await TransitiveProtectSelfReferential.objects.filter(pk=guarded_root.pk).delete()
    assert exc_info.value.protected_objects == [guard]

    roots = [guarded_root.pk, other_root.pk]
    assert await TransitiveProtectSelfReferential.objects.filter(pk__in=roots).delete() == 2
    assert not await TransitiveProtectSelfReferential.objects.filter(pk=guard.pk).exists()
