"""A correlated EXISTS on ClickHouse is written as an IN test of the columns it is correlated by - a
composite key's row of columns too: a filter across a many-to-many relation to a model of a tenant,
keyed by two columns, under the tenant's scope. One correlated by a comparison is the server's own
EXISTS where the server runs correlated subqueries."""

import uuid

import pytest

from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Exists, OuterReference, Subquery
from hare.query.functions import Count
from tests.dialects.clickhouse.models import PageVersion, Player, Team, TenantDocument, TenantLabel


@pytest.mark.asyncio
async def test_a_many_to_many_filter_of_a_tenant_reads_a_composite_key(clickhouse_db):
    with Tenancy.scope(1):
        label = await TenantLabel.objects.create(code="a1", region="x")
        document = await TenantDocument.objects.create(id=11, title="t")
        await document.labels.add(label)
        assert await TenantDocument.objects.filter(labels__code="a1").count() == 1
        assert await TenantDocument.objects.filter(labels=label).count() == 1
    with Tenancy.scope(2):
        assert await TenantDocument.objects.all_tenants().filter(labels__code="a1").count() == 1


@pytest.mark.asyncio
async def test_a_subquery_correlated_by_a_comparison_is_the_servers_own_exists(clickhouse_db):
    red = await Team.objects.create(name="red")
    await Player.objects.bulk_create(
        [Player(id=1, name="ann", team=red), Player(id=2, name="bob", team=red), Player(id=3, name="cid")]
    )
    # The same table inside and around - the outer one's name reads the outer row.
    later_of_team = Player.objects.filter(team=OuterReference("team"), id__gt=OuterReference("id"))
    latest = Player.objects.filter(~Exists(later_of_team)).order_by("id").values_list("id", flat=True)
    if not Player._meta.connection.features.supports_correlated_subqueries:
        with pytest.raises(UnSupportedError, match="has no correlated subqueries"):
            await latest
        return
    # A NULL team equals none - the player without one has no later player of the team.
    assert await latest == [2, 3]
    assert await Player.objects.filter(Exists(Player.objects.filter(id__lt=OuterReference("id")))).count() == 2


@pytest.mark.requires("request-query")
@pytest.mark.asyncio
async def test_latest_versions_keep_the_latest_version_of_each_record(clickhouse_db):
    from hare.contrib.request_query import LatestVersions, RequestQuery

    class LatestPageQuery(RequestQuery[PageVersion]):
        class Meta:
            queryset = PageVersion.objects.all()
            versions = LatestVersions()

    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    await PageVersion.objects.bulk_create(
        [
            PageVersion(id=first, version=1, title="a1"),
            PageVersion(id=first, version=2, title="a2"),
            PageVersion(id=second, version=1, title="b1"),
        ]
    )
    if not PageVersion._meta.connection.features.supports_correlated_subqueries:
        with pytest.raises(ConfigurationError, match="runs no correlated subqueries"):
            LatestPageQuery.check_declaration()
        return
    LatestPageQuery.check_declaration()
    assert sorted(page.title for page in await LatestPageQuery().fetch()) == ["a2", "b1"]


@pytest.mark.asyncio
async def test_a_subquery_correlated_by_a_comparison_is_refused_in_a_mutation(clickhouse_db):
    earlier = Player.objects.filter(id__lt=OuterReference("id"))
    with pytest.raises(UnSupportedError, match="no correlated subquery in an UPDATE or a DELETE"):
        await Player.objects.filter(Exists(earlier)).update(name="x")


@pytest.mark.asyncio
async def test_an_exists_of_a_union_is_an_exists_of_either_part(clickhouse_db):
    red, blue, green = await Team.objects.bulk_create([Team(name="red"), Team(name="blue"), Team(name="green")])
    await Player.objects.bulk_create(
        [Player(id=1, name="ann", team=red), Player(id=2, name="bob", team=blue), Player(id=3, name="cid", team=green)]
    )
    named = Player.objects.filter(team=OuterReference("pk"), name="ann").union(
        Player.objects.filter(team=OuterReference("pk"), name="bob")
    )
    assert await Team.objects.filter(Exists(named)).order_by("name").values_list("name", flat=True) == ["blue", "red"]


@pytest.mark.asyncio
async def test_a_grouping_by_a_correlated_subquery_is_refused(clickhouse_db):
    teams_named = Team.objects.filter(pk=OuterReference("team")).values("name")
    with pytest.raises(UnSupportedError, match="groups rows by no correlated subquery"):
        await Player.objects.annotate(team_name=Subquery(teams_named)).values("team_name").annotate(n=Count("id"))
