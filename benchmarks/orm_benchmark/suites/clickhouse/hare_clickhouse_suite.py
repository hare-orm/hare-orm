from __future__ import annotations

import datetime
import uuid
from typing import Any

from orm_benchmark.suites.clickhouse.clickhouse_suite import ClickhouseSuite


class HareClickhouseSuite(ClickhouseSuite):
    """The scenarios on hare-orm's ClickHouse dialect, through ``Model.objects`` - over clickhouse-connect
    (HTTP) or, for the ``hare-clickhouse-driver`` target, over clickhouse-driver (the native TCP
    protocol)."""

    MODELS_MODULE = "orm_benchmark.suites.clickhouse.hare_clickhouse_models"

    def load(self) -> None:
        import hare  # noqa: F401

    async def connect(self) -> None:
        from hare import Hare

        database = self.database
        if self.target.key == "hare-clickhouse-driver":
            scheme, port = "clickhouse+clickhouse-driver", database.native_port
        else:
            scheme, port = "clickhouse+clickhouse-connect", database.http_port
        await Hare.init(
            config={
                "connections": {"default": f"{scheme}://default:{database.password}@127.0.0.1:{port}/{database.name}"},
                "apps": {"models": {"models": [self.MODELS_MODULE], "default_connection": "default"}},
            }
        )
        from orm_benchmark.suites.clickhouse.hare_clickhouse_models import Event

        self.events = Event.objects
        self.event_class = Event
        await Event.get_connection().execute_dicts("SELECT 1")

    async def close(self) -> None:
        from hare import Hare

        await Hare.close_connections()

    async def create_table(self) -> None:
        from hare import Hare

        await Hare.generate_schemas()

    async def insert(self, rows: list[dict[str, Any]]) -> None:
        await self.events.bulk_create([self.event_class(**row) for row in rows])

    async def count_filter(self) -> None:
        await self.events.filter(event_type="buy", amount__gt=50).count()

    async def group_sum(self) -> None:
        from hare.query.functions import Avg, Sum

        await self.events.values("site").annotate(total=Sum("amount"), average=Avg("amount"))

    async def top_n(self) -> None:
        from hare.query.functions import Sum

        await self.events.values("user_id").annotate(total=Sum("amount")).order_by("-total").limit(10)

    async def by_day(self) -> None:
        from hare.query.functions import Count, Trunc

        await (
            self.events.annotate(day=Trunc("happened_at", "day"))
            .values("day")
            .annotate(events=Count("id"))
            .order_by("day")
        )

    async def distinct_count(self) -> None:
        from hare.query.functions import Count

        await self.events.all().aggregate(users=Count("user_id", distinct=True))

    async def page_values(self) -> None:
        await self.events.order_by("id").offset(1000).limit(100).values("id", "site", "amount")

    async def get_by_key(self, key: uuid.UUID) -> None:
        await self.events.get(id=key)

    async def update_mutation(self) -> None:
        await self.events.filter(event_type="share").update(amount=0)

    async def delete_mutation(self, site: str) -> None:
        await self.events.filter(site=site).delete()

    async def count_day(self, day: datetime.datetime) -> None:
        from hare.query.functions import Count

        rows = self.events.filter(happened_at__gte=day, happened_at__lt=day + datetime.timedelta(days=1))
        await rows.values("event_type").annotate(events=Count("id"))
