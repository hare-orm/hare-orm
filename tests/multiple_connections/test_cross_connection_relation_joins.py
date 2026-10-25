"""Regression coverage for ForeignKeyField/relation lookups crossing a multi-connection
(cross-app) boundary via a real SQL JOIN - see LookupPaths.get_joins_for_related_field()'s own
connection-mismatch guard in hare/query/utils.py.

Uses two throwaway, dynamically-registered model modules (injected into sys.modules) instead of
tests/shared_models/testmodels.py - that module is shared by nearly the whole suite under a single-app config,
and a genuine cross-app model reference there breaks collection for everything else that loads it.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields, prefetch_related_objects
from hare.exceptions import (
    QueryError,
)
from hare.models import Model
from hare.query.expressions import Exists, OuterReference, Subquery
from tests.utils.multi_database_context import MultiDatabaseTestContext

OWNER_MODULE_NAME = "tests._cross_conn_owner_models"
TARGET_MODULE_NAME = "tests._cross_conn_target_models"


@pytest_asyncio.fixture
async def cross_conn_fk():
    class CrossConnTarget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "ccj_target"

    class CrossConnOwner(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        # db_constraint=False isolates this from the separate, known cross-database
        # FK-constraint limitation - only the JOIN-across-connections guard is under test here.
        target: fields.ForeignKeyNullableRelation[CrossConnTarget] = fields.ForeignKeyField(
            "ccj_target.CrossConnTarget", null=True, db_constraint=False
        )

        class Meta:
            app = "ccj_owner"

    owner_module = types.ModuleType(OWNER_MODULE_NAME)
    setattr(owner_module, "CrossConnOwner", CrossConnOwner)  # noqa: B010
    target_module = types.ModuleType(TARGET_MODULE_NAME)
    setattr(target_module, "CrossConnTarget", CrossConnTarget)  # noqa: B010
    sys.modules[OWNER_MODULE_NAME] = owner_module
    sys.modules[TARGET_MODULE_NAME] = target_module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["ccj_owner", "ccj_target"],
            apps={
                "ccj_owner": {"models": [OWNER_MODULE_NAME], "default_connection": "ccj_owner"},
                "ccj_target": {"models": [TARGET_MODULE_NAME], "default_connection": "ccj_target"},
            },
        ) as ctx:
            await ctx.generate_schemas()
            yield CrossConnOwner, CrossConnTarget
    finally:
        sys.modules.pop(OWNER_MODULE_NAME, None)
        sys.modules.pop(TARGET_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_fetch_related_works_across_connections(cross_conn_fk):
    """The one-query-per-connection relation path stays unaffected by the JOIN guard below."""
    CrossConnOwner, CrossConnTarget = cross_conn_fk
    target = await CrossConnTarget.objects.create(name="Target")
    owner = await CrossConnOwner.objects.create(name="Owner", target=target)

    await prefetch_related_objects([owner], "target")
    assert owner.target.name == "Target"


@pytest.mark.asyncio
async def test_a_prefetch_under_using_reads_a_related_model_of_another_database_on_that_database(cross_conn_fk):
    """The connection the parent query pinned with using() was offered to the related model of another
    default connection too - its rows were looked for in the parent's database, "no such table"."""
    CrossConnOwner, CrossConnTarget = cross_conn_fk
    target = await CrossConnTarget.objects.create(name="Target")
    await CrossConnOwner.objects.create(name="Owner", target=target)

    owners = await CrossConnOwner.objects.using("ccj_owner").prefetch_related("target")
    assert [owner.target.name for owner in owners] == ["Target"]


@pytest.mark.parametrize(
    "make_query",
    [
        pytest.param(
            lambda query: query.select_related("target"),
            id="select_related_across_connections_raises_configuration_error",
        ),
        # order_by("relation__field") resolves through the identical JOIN-building path.
        pytest.param(
            lambda query: query.order_by("target__name"),
            id="order_by_related_field_across_connections_raises_configuration_error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_join_across_connections_raises_configuration_error(cross_conn_fk, make_query):
    """A JOIN used to be built between the owner's and the target's tables with no check that they
    even live on the same connection - CrossConnOwner and CrossConnTarget are on two separate
    connections here, so the JOIN referenced a table that doesn't exist on whichever connection
    actually ran the query, surfacing as a raw "no such table"/"relation does not exist"."""
    CrossConnOwner, _ = cross_conn_fk
    with pytest.raises(QueryError, match="different database connection"):
        await make_query(CrossConnOwner.objects.all()).first()


@pytest.mark.asyncio
async def test_only_related_field_across_connections_raises_configuration_error(cross_conn_fk):
    """.only("relation__field") goes through the same JOIN-building path as select_related()."""
    CrossConnOwner, _ = cross_conn_fk
    with pytest.raises(QueryError, match="different database connection"):
        await CrossConnOwner.objects.all().only("id", "target__name").first()


@pytest.mark.asyncio
async def test_filter_across_relation_crossing_connections_raises_configuration_error(cross_conn_fk):
    """A `relation__field` filter lookup resolves through the identical JOIN-building path."""
    CrossConnOwner, _ = cross_conn_fk
    with pytest.raises(QueryError, match="different database connection"):
        await CrossConnOwner.objects.filter(target__name="Target").first()


@pytest.mark.asyncio
async def test_union_across_connections_raises_configuration_error(cross_conn_fk):
    """union()/intersection()/difference() glue every branch's SELECT text into one SQL string
    and execute it against a single connection - a branch resolving to a DIFFERENT connection
    used to go unchecked, either surfacing as a raw "no such table"/"relation does not exist" (two
    genuinely separate databases) or, worse, silently re-querying the SAME table for every branch
    with no error at all (two same-schema connections, e.g. a sharded model queried via
    .using("shard_a")/.using("shard_b")) - duplicated rows from one branch, the other branch's
    rows never appearing."""
    CrossConnOwner, CrossConnTarget = cross_conn_fk
    await CrossConnOwner.objects.create(name="Owner")
    await CrossConnTarget.objects.create(name="Target")

    with pytest.raises(QueryError, match="different database connection"):
        await CrossConnOwner.objects.all().only("id", "name").union(CrossConnTarget.objects.all().only("id", "name"))


@pytest.mark.parametrize(
    "make_query",
    [
        pytest.param(
            lambda owner, target: owner.objects.annotate(
                name_of_target=Subquery(target.objects.filter(id=OuterReference("target_id")).values("name"))
            ),
            id="subquery",
        ),
        pytest.param(
            lambda owner, target: owner.objects.filter(Exists(target.objects.filter(id=OuterReference("target_id")))),
            id="exists",
        ),
        pytest.param(
            lambda owner, target: owner.objects.filter(target_id__in=target.objects.values("id")), id="in_queryset"
        ),
        pytest.param(lambda owner, target: owner.objects.with_cte("picked", target.objects.all()), id="with_cte"),
    ],
)
@pytest.mark.asyncio
async def test_a_query_of_a_model_on_another_connection_embedded_is_refused(cross_conn_fk, make_query):
    """Only a query pinned by .using() was refused - one of a model living on another connection was bound to
    the outer one and read that connection's table of its name, or failed on no such table."""
    CrossConnOwner, CrossConnTarget = cross_conn_fk
    with pytest.raises(QueryError, match="different database connection"):
        await make_query(CrossConnOwner, CrossConnTarget).values_list("id", flat=True)
