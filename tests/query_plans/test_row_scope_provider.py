"""Explicit providers bind at execution and never require a related queryset factory."""

from unittest.mock import patch

import pytest

from hare.query.expressions import ConditionValue, Exists, OuterReference, Q
from tests.shared_models.testmodels import OwnerScopedRows, OwnerScopedSecret, OwnerScopedSecretRef


@pytest.mark.asyncio
@pytest.mark.plan_verification_after_test
@pytest.mark.parametrize("shape", ["root", "join", "prefetch"])
@pytest.mark.parametrize("predicate", ["q", "condition_value", "exists"])
async def test_reused_query_binds_provider_once_per_execution(db, shape, predicate):
    refs_by_owner = {}
    for name in ("alice", "bob"):
        secret = await OwnerScopedSecret.objects.create(owner=name, text=name)
        ref = await OwnerScopedSecretRef.objects.create(secret=secret)
        refs_by_owner[name] = ref.id
    if shape == "root":
        query = OwnerScopedSecret.objects.order_by("id")
    elif shape == "join":
        query = OwnerScopedSecretRef.objects.order_by("id").select_related("secret")
    else:
        query = OwnerScopedSecretRef.objects.order_by("id").prefetch_related("secret")
    provider = OwnerScopedSecret._meta.manager.scope

    def get_predicate(model, visibility):
        owner = OwnerScopedRows.current_owner.get()
        condition = Q(owner=owner)
        if predicate == "condition_value":
            return ConditionValue(condition)
        if predicate == "exists":
            # This unscoped subquery uses stored FK columns, never the scoped secret's provider.
            return Exists(OwnerScopedSecretRef.objects.filter(id=refs_by_owner[owner], secret_id=OuterReference("id")))
        return condition

    with patch.object(provider, "get_condition", side_effect=get_predicate) as get_condition:
        for name in ("alice", "bob", "alice"):
            token = OwnerScopedRows.current_owner.set(name)
            get_condition.reset_mock()
            try:
                rows = await query
            finally:
                OwnerScopedRows.current_owner.reset(token)
            values = [row.text for row in rows] if shape == "root" else [row.secret.text for row in rows if row.secret]
            assert values == [name]
            get_condition.assert_called_once()


@pytest.mark.asyncio
async def test_join_policy_does_not_call_queryset_factory(db):
    manager = OwnerScopedSecret._meta.manager
    token = OwnerScopedRows.current_owner.set("alice")
    try:
        secret = await OwnerScopedSecret.objects.create(owner="alice", text="visible")
        await OwnerScopedSecretRef.objects.create(secret=secret)
        with patch.object(manager, "get_queryset", side_effect=AssertionError("queryset factory used for a JOIN")):
            rows = await OwnerScopedSecretRef.objects.select_related("secret")
        assert rows[0].secret.text == "visible"
    finally:
        OwnerScopedRows.current_owner.reset(token)
