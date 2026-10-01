"""The query-shape cache must never freeze a related model's runtime-dependent ``Meta.manager``
scope into a cached JOIN - every relation-crossing shape is re-scoped on every call."""

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

import pytest

from hare.contrib import test
from hare.models.tenancy import Tenancy
from hare.query.expressions import F, OuterRef, Subquery
from hare.query.functions import Count, Max
from hare.query.plans.statement_plans import StatementPlans
from tests.testmodels import (
    OptionalOwnerScopedSecret,
    OwnerScopedLabel,
    OwnerScopedManager,
    OwnerScopedPlayer,
    OwnerScopedRoster,
    OwnerScopedSecret,
    OwnerScopedSecretRef,
    OwnerScopedTeam,
    SharedOrOwnTemplate,
)


@contextmanager
def _owner(owner: str | None):
    token = OwnerScopedManager.current_owner.set(owner)
    try:
        yield
    finally:
        OwnerScopedManager.current_owner.reset(token)


async def _create_secrets() -> None:
    await OwnerScopedSecret.objects.create(id=1, owner="alice", text="ALICE-SECRET")
    await OwnerScopedSecret.objects.create(id=2, owner="bob", text="BOB-SECRET")
    await OwnerScopedSecretRef.objects.create(id=1, secret_id=1)
    await OwnerScopedSecretRef.objects.create(id=2, secret_id=2)


async def _assert_alternating_owners(
    factory: Callable[[], Awaitable[Any]],
    expected_by_owner: dict[str, Any],
    normalize: Callable[[Any], Any] | None = None,
) -> None:
    StatementPlans.plans.clear()
    for owner in ("alice", "bob", "alice", "bob"):
        with _owner(owner):
            result = await factory()
        if normalize is not None:
            result = normalize(result)
        assert result == expected_by_owner[owner], owner


def _nulls_sort_first(model: Any) -> bool:
    return model._meta.db.dialect.name != "postgresql"


@pytest.mark.asyncio
async def test_values_list_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().order_by("id").values_list("id", "secret__text"),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
    )


@pytest.mark.asyncio
async def test_values_named_path_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().order_by("id").values("id", text="secret__text"),
        {
            "alice": [{"id": 1, "text": "ALICE-SECRET"}, {"id": 2, "text": None}],
            "bob": [{"id": 1, "text": None}, {"id": 2, "text": "BOB-SECRET"}],
        },
    )


@pytest.mark.asyncio
async def test_values_f_expression_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().order_by("id").values("id", text=F("secret__text")),
        {
            "alice": [{"id": 1, "text": "ALICE-SECRET"}, {"id": 2, "text": None}],
            "bob": [{"id": 1, "text": None}, {"id": 2, "text": "BOB-SECRET"}],
        },
    )


@pytest.mark.asyncio
async def test_annotation_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().annotate(text=F("secret__text")).order_by("id"),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
        lambda refs: [(ref.id, ref.text) for ref in refs],
    )


@pytest.mark.asyncio
async def test_only_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().only("id", "secret__text").order_by("id"),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
        lambda refs: [(ref.id, ref.secret.text if ref.secret else None) for ref in refs],
    )


@pytest.mark.asyncio
async def test_select_related_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().select_related("secret").order_by("id"),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
        lambda refs: [(ref.id, ref.secret.text if ref.secret else None) for ref in refs],
    )


@pytest.mark.asyncio
async def test_select_related_across_scope_whose_shape_changes_per_call(db):
    """Unscoped on the first call (no owner) - the next, owner-scoped call must not reuse its JOIN."""
    await OptionalOwnerScopedSecret.objects.create(id=1, owner="alice", text="ALICE-SECRET")
    await OptionalOwnerScopedSecret.objects.create(id=2, owner="bob", text="BOB-SECRET")
    await OwnerScopedSecretRef.objects.create(id=1, optional_secret_id=1)
    await OwnerScopedSecretRef.objects.create(id=2, optional_secret_id=2)
    StatementPlans.plans.clear()
    expected_by_owner = {
        None: [(1, "ALICE-SECRET"), (2, "BOB-SECRET")],
        "alice": [(1, "ALICE-SECRET"), (2, None)],
        "bob": [(1, None), (2, "BOB-SECRET")],
    }
    for owner in (None, "alice", None, "bob"):
        with _owner(owner):
            refs = await OwnerScopedSecretRef.objects.all().select_related("optional_secret").order_by("id")
            values = await OwnerScopedSecretRef.objects.all().order_by("id").values_list("id", "optional_secret__text")
        assert [(ref.id, ref.optional_secret.text if ref.optional_secret else None) for ref in refs] == (
            expected_by_owner[owner]
        )
        assert values == expected_by_owner[owner]


@pytest.mark.asyncio
async def test_order_by_across_runtime_scoped_relation(db):
    await _create_secrets()
    nulls_first = _nulls_sort_first(OwnerScopedSecretRef)
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().order_by("secret__text").values_list("id", flat=True),
        {"alice": [2, 1] if nulls_first else [1, 2], "bob": [1, 2] if nulls_first else [2, 1]},
    )


@pytest.mark.asyncio
async def test_group_by_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: (
            OwnerScopedSecretRef.objects.all()
            .annotate(rows=Count("id"))
            .group_by("secret__text")
            .values_list("secret__text", "rows")
        ),
        {"alice": {("ALICE-SECRET", 1), (None, 1)}, "bob": {("BOB-SECRET", 1), (None, 1)}},
        set,
    )


@pytest.mark.asyncio
async def test_distinct_values_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().values_list("secret__text", flat=True).distinct(),
        {"alice": {"ALICE-SECRET", None}, "bob": {"BOB-SECRET", None}},
        set,
    )


@pytest.mark.asyncio
async def test_aggregate_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().aggregate(top=Max("secret__text")),
        {"alice": {"top": "ALICE-SECRET"}, "bob": {"top": "BOB-SECRET"}},
    )


@pytest.mark.asyncio
async def test_aggregate_over_distinct_values_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: (
            OwnerScopedSecretRef.objects.all().values(text=F("secret__text")).distinct().aggregate(top=Max("text"))
        ),
        {"alice": {"top": "ALICE-SECRET"}, "bob": {"top": "BOB-SECRET"}},
    )


@pytest.mark.asyncio
async def test_count_and_exists_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.filter(secret__text="ALICE-SECRET").count(),
        {"alice": 1, "bob": 0},
    )
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.filter(secret__text="BOB-SECRET").exists(),
        {"alice": False, "bob": True},
    )


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: (
            OwnerScopedSecretRef.objects.all()
            .order_by("secret__text")
            .distinct("secret__text")
            .values_list("secret__text", flat=True)
        ),
        {"alice": {"ALICE-SECRET", None}, "bob": {"BOB-SECRET", None}},
        set,
    )


@pytest.mark.asyncio
async def test_m2m_values_across_runtime_scoped_target(db):
    await _create_secrets()
    label = await OwnerScopedLabel.objects.create(id=1, name="shared")
    with _owner("alice"):
        await (await OwnerScopedSecret.objects.get(id=1)).labels.add(label)
    with _owner("bob"):
        await (await OwnerScopedSecret.objects.get(id=2)).labels.add(label)
    await _assert_alternating_owners(
        lambda: OwnerScopedLabel.objects.all().values_list("id", "secrets__text"),
        {"alice": [(1, "ALICE-SECRET")], "bob": [(1, "BOB-SECRET")]},
    )


@pytest.mark.asyncio
async def test_m2m_values_across_runtime_scoped_through_model(db):
    team = await OwnerScopedTeam.objects.create(id=1, name="team")
    alice_player = await OwnerScopedPlayer.objects.create(id=1, name="ALICE-PLAYER")
    bob_player = await OwnerScopedPlayer.objects.create(id=2, name="BOB-PLAYER")
    await OwnerScopedRoster.objects.create(id=1, team=team, player=alice_player, owner="alice")
    await OwnerScopedRoster.objects.create(id=2, team=team, player=bob_player, owner="bob")
    await _assert_alternating_owners(
        lambda: OwnerScopedTeam.objects.all().values_list("id", "players__name"),
        {"alice": [(1, "ALICE-PLAYER")], "bob": [(1, "BOB-PLAYER")]},
    )


@pytest.mark.asyncio
async def test_tenant_reading_manager_scope_across_relation(db):
    await SharedOrOwnTemplate.objects.create(id=1, company_id=None, body="GLOBAL")
    await SharedOrOwnTemplate.objects.create(id=2, company_id=1, body="TENANT1-PRIVATE")
    await SharedOrOwnTemplate.objects.create(id=3, company_id=2, body="TENANT2-PRIVATE")
    for ref_id in (1, 2, 3):
        await OwnerScopedSecretRef.objects.create(id=ref_id, template_id=ref_id)
    expected_by_tenant = {
        1: [(1, "GLOBAL"), (2, "TENANT1-PRIVATE"), (3, None)],
        2: [(1, "GLOBAL"), (2, None), (3, "TENANT2-PRIVATE")],
    }
    StatementPlans.plans.clear()
    for tenant in (1, 2, 1, 2):
        with Tenancy.scope(tenant):
            rows = await OwnerScopedSecretRef.objects.all().order_by("id").values_list("id", "template__body")
            refs = await OwnerScopedSecretRef.objects.all().only("id", "template__body").order_by("id")
            top = await OwnerScopedSecretRef.objects.all().aggregate(top=Max("template__body"))
        assert rows == expected_by_tenant[tenant]
        assert [(ref.id, ref.template.body if ref.template else None) for ref in refs] == expected_by_tenant[tenant]
        assert top == {"top": f"TENANT{tenant}-PRIVATE"}


@pytest.mark.asyncio
async def test_subquery_across_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: (
            OwnerScopedSecretRef.objects.all()
            .annotate(text=Subquery(OwnerScopedSecretRef.objects.filter(id=OuterRef("id")).values("secret__text")))
            .order_by("id")
            .values_list("id", "text")
        ),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
    )


@pytest.mark.asyncio
async def test_prefetch_of_runtime_scoped_relation(db):
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecretRef.objects.all().prefetch_related("secret").order_by("id"),
        {"alice": [(1, "ALICE-SECRET"), (2, None)], "bob": [(1, None), (2, "BOB-SECRET")]},
        lambda refs: [(ref.id, ref.secret.text if ref.secret else None) for ref in refs],
    )


@pytest.mark.asyncio
async def test_direct_query_on_runtime_scoped_model_stays_cached_and_correct(db):
    """The model's own manager filter lives in the query's own filters - still cached, re-bound per call."""
    await _create_secrets()
    await _assert_alternating_owners(
        lambda: OwnerScopedSecret.objects.all().values_list("text", flat=True),
        {"alice": ["ALICE-SECRET"], "bob": ["BOB-SECRET"]},
    )
    assert len(StatementPlans.plans) >= 1
