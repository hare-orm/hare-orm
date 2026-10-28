"""ClickhouseRequestQuery: a request query checked against ClickHouse only, so its filters may be on the fields
only ClickHouse has - the base RequestQuery refuses them, being checked on SQLite and PostgreSQL too."""

import pytest

from hare.contrib.request_query import RequestQuery
from hare.dialects.clickhouse.request_query import ClickhouseRequestQuery
from hare.exceptions import ConfigurationError
from tests.dialects.clickhouse.models import Device

REQUIRES = ("request-query",)


@pytest.mark.asyncio
async def test_a_clickhouse_request_query_filters_by_the_dialects_fields(clickhouse_db):
    await Device.objects.create(
        id=1,
        tiny=3,
        big=2**63,
        ratio=1.5,
        code="abcd",
        country="DE",
        status="online",
        address="10.0.0.1",
        labels=["a"],
    )
    await Device.objects.create(
        id=2, tiny=4, big=1, ratio=2.5, code="efgh", country="FR", status="online", address="10.0.0.2", labels=[]
    )

    class DeviceQuery(ClickhouseRequestQuery[Device]):
        tiny: int | None = None
        country: str | None = None

        class Meta:
            queryset = Device.objects.all()

    assert [device.id for device in await DeviceQuery.model_validate({"tiny": 3}).fetch()] == [1]
    assert [device.id for device in await DeviceQuery.model_validate({"country": "FR"}).fetch()] == [2]


@pytest.mark.asyncio
async def test_the_base_request_query_refuses_the_dialects_fields(clickhouse_db):
    class BaseDeviceQuery(RequestQuery[Device]):
        tiny: int | None = None

        class Meta:
            queryset = Device.objects.all()

    with pytest.raises(ConfigurationError, match="doesn't run on"):
        BaseDeviceQuery.get_declaration()
