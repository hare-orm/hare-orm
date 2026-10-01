"""FK on_delete cascade actions (CASCADE/SET_NULL/RESTRICT/PROTECT) across a multi-connection
(cross-app) boundary. Mirrors the exact pattern of tests/test_m2m_cross_connection.py and
tests/test_cross_connection_relation_joins.py (dynamically-registered modules, two real
connections, db_constraint=False to isolate from the separate/known cross-db FK-constraint DDL
limitation).

ReverseRelationCascade used to always force the ROOT instance's own connection onto every
cascade step, even for a related model living on a genuinely different connection - the related
query/write then ran against the wrong physical database entirely ("no such table"/"relation
does not exist", or worse, silently against a same-named table in the WRONG database if both
connections happen to share identical schema). fetch_related()/prefetch_related()/M2M add() all
already resolved each side's own connection correctly; cascade.py was the one write path that
didn't.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest
import pytest_asyncio

from hare import fields
from hare.exceptions import IntegrityError, ProtectedError
from hare.fields.constants import CASCADE, PROTECT, RESTRICT, SET_NULL
from hare.models import Model
from tests.utils.multi_database_context import MultiDatabaseTestContext

PARENT_MODULE_NAME = "tests._ccx_parent_models"
CHILD_MODULE_NAME = "tests._ccx_child_models"


def _build_cross_connection_models(on_delete: Any) -> tuple[type[Model], type[Model]]:
    class CcxParent(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "ccx_parent"

    class CcxChild(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        # db_constraint=False isolates this from the separate, known cross-database
        # FK-constraint DDL limitation - only the cascade-connection bug is under test here.
        parent: fields.ForeignKeyRelation[CcxParent] = fields.ForeignKeyField(  # type: ignore[call-overload]
            "ccx_parent.CcxParent", db_constraint=False, on_delete=on_delete, null=(on_delete is SET_NULL)
        )

        class Meta:
            app = "ccx_child"

    return CcxParent, CcxChild


@pytest_asyncio.fixture
async def cross_conn_cascade(request):
    on_delete = getattr(request, "param", CASCADE)
    CcxParent, CcxChild = _build_cross_connection_models(on_delete)

    parent_module = types.ModuleType(PARENT_MODULE_NAME)
    setattr(parent_module, "CcxParent", CcxParent)  # noqa: B010
    child_module = types.ModuleType(CHILD_MODULE_NAME)
    setattr(child_module, "CcxChild", CcxChild)  # noqa: B010
    sys.modules[PARENT_MODULE_NAME] = parent_module
    sys.modules[CHILD_MODULE_NAME] = child_module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["ccx_parent", "ccx_child"],
            apps={
                "ccx_parent": {"models": [PARENT_MODULE_NAME], "default_connection": "ccx_parent"},
                "ccx_child": {"models": [CHILD_MODULE_NAME], "default_connection": "ccx_child"},
            },
        ) as ctx:
            await ctx.generate_schemas()
            yield ctx, CcxParent, CcxChild
    finally:
        sys.modules.pop(PARENT_MODULE_NAME, None)
        sys.modules.pop(CHILD_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_cascade_delete_across_connections(cross_conn_cascade):
    """Deleting the parent should CASCADE the delete onto the child even though the child
    lives on a totally separate connection/database - this is exactly the scenario
    db_constraint=False + on_delete=CASCADE exists to support in a multi-connection setup
    (fetch_related()/prefetch_related()/M2M add() all already handle this boundary correctly).
    """
    _ctx, CcxParent, CcxChild = cross_conn_cascade
    parent = await CcxParent.objects.create(name="P")
    await CcxChild.objects.create(name="C", parent=parent)

    await parent.delete()

    remaining = await CcxChild.objects.all()
    assert remaining == [], (
        f"child row should have been cascade-deleted on its OWN connection, still present: {remaining}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_conn_cascade", [SET_NULL], indirect=True)
async def test_set_null_across_connections(cross_conn_cascade):
    """on_delete=SET_NULL must null out the child's shadow FK column on the CHILD's own
    connection, not crash trying to run the UPDATE against the parent's connection."""
    _ctx, CcxParent, CcxChild = cross_conn_cascade
    parent = await CcxParent.objects.create(name="P")
    child = await CcxChild.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await CcxChild.objects.get(pk=child.pk)
    assert refreshed.parent_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_conn_cascade", [RESTRICT], indirect=True)
async def test_restrict_across_connections_blocks_the_delete(cross_conn_cascade):
    """on_delete=RESTRICT must correctly detect the live child row on ITS OWN connection and
    block the parent's delete - not crash with "no such table" while trying to check it against
    the parent's connection, and not silently allow the delete through because the (wrong-
    connection) existence check found nothing."""
    _ctx, CcxParent, CcxChild = cross_conn_cascade
    parent = await CcxParent.objects.create(name="P")
    await CcxChild.objects.create(name="C", parent=parent)

    with pytest.raises(IntegrityError):
        await parent.delete()

    assert await CcxParent.objects.filter(pk=parent.pk).exists(), (
        "parent must survive - the RESTRICT check should have blocked the delete"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_conn_cascade", [PROTECT], indirect=True)
async def test_protect_across_connections_blocks_the_delete(cross_conn_cascade):
    """on_delete=PROTECT's check_protected() must correctly see the live child row on ITS OWN
    connection - not crash checking it against the parent's connection, and not silently allow
    the delete through because the (wrong-connection) existence check found nothing."""
    _ctx, CcxParent, CcxChild = cross_conn_cascade
    parent = await CcxParent.objects.create(name="P")
    await CcxChild.objects.create(name="C", parent=parent)

    with pytest.raises(ProtectedError):
        await parent.delete()

    assert await CcxParent.objects.filter(pk=parent.pk).exists(), (
        "parent must survive - the PROTECT check should have blocked the delete"
    )
