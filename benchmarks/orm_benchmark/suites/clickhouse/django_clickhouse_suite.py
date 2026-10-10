from __future__ import annotations

import datetime
import uuid
from typing import Any

from orm_benchmark.suites.clickhouse.clickhouse_suite import ClickhouseSuite


class DjangoClickhouseSuite(ClickhouseSuite):
    """The scenarios on Django's async ORM with django-clickhouse-backend over clickhouse-driver
    (ClickHouse's native protocol); ``update()`` and ``delete()`` run as ClickHouse's mutations."""

    def load(self) -> None:
        import django
        from django.conf import settings

        database = self.database
        settings.configure(
            DEBUG=False,
            DATABASES={
                "default": {
                    "ENGINE": "clickhouse_backend.backend",
                    "NAME": database.name,
                    "HOST": "127.0.0.1",
                    "PORT": database.native_port,
                    "USER": "default",
                    "PASSWORD": database.password,
                    # A mutation waits for the rows it changes, as hare's does.
                    "OPTIONS": {"settings": {"mutations_sync": 1}},
                }
            },
            INSTALLED_APPS=["clickhouse_backend", "orm_benchmark.suites.django_orm"],
            USE_TZ=True,
            DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        )
        django.setup()
        from clickhouse_backend import models

        class DjangoEvent(models.ClickhouseModel):
            id = models.UUIDField(primary_key=True)
            site = models.StringField()
            user_id = models.Int32Field()
            amount = models.Float64Field()
            happened_at = models.DateTime64Field(precision=6)
            event_type = models.StringField()

            class Meta:
                app_label = "django_orm"
                db_table = "event"
                engine = models.MergeTree(order_by=("id",))

        self.event_class = DjangoEvent
        self.events = DjangoEvent.objects

    async def connect(self) -> None:
        await self.select_one()

    async def select_one(self) -> None:
        from asgiref.sync import sync_to_async
        from django.db import connection

        def run() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchall()

        await sync_to_async(run)()

    async def close(self) -> None:
        from asgiref.sync import sync_to_async
        from django.db import connections

        def close_connections() -> None:
            for database_connection in connections.all():
                database_connection.close()

        await sync_to_async(close_connections)()

    async def create_table(self) -> None:
        from asgiref.sync import sync_to_async
        from django.db import connection

        def create() -> None:
            with connection.schema_editor() as editor:
                editor.create_model(self.event_class)

        await sync_to_async(create)()

    async def insert(self, rows: list[dict[str, Any]]) -> None:
        await self.events.abulk_create([self.event_class(**row) for row in rows])

    async def count_filter(self) -> None:
        await self.events.filter(event_type="buy", amount__gt=50).acount()

    async def group_sum(self) -> None:
        from django.db.models import Avg, Sum

        [row async for row in self.events.values("site").annotate(total=Sum("amount"), average=Avg("amount"))]

    async def top_n(self) -> None:
        from django.db.models import Sum

        [row async for row in self.events.values("user_id").annotate(total=Sum("amount")).order_by("-total")[:10]]

    async def by_day(self) -> None:
        from django.db.models import Count
        from django.db.models.functions import TruncDay

        rows = (
            self.events.annotate(day=TruncDay("happened_at"))
            .values("day")
            .annotate(events=Count("id"))
            .order_by("day")
        )
        [row async for row in rows]

    async def distinct_count(self) -> None:
        from django.db.models import Count

        await self.events.aaggregate(users=Count("user_id", distinct=True))

    async def page_values(self) -> None:
        [row async for row in self.events.order_by("id").values("id", "site", "amount")[1000:1100]]

    async def get_by_key(self, key: uuid.UUID) -> None:
        await self.events.aget(id=key)

    async def update_mutation(self) -> None:
        await self.events.filter(event_type="share").aupdate(amount=0)

    async def delete_mutation(self, site: str) -> None:
        await self.events.filter(site=site).adelete()

    async def count_day(self, day: datetime.datetime) -> None:
        from django.db.models import Count

        rows = self.events.filter(happened_at__gte=day, happened_at__lt=day + datetime.timedelta(days=1))
        [row async for row in rows.values("event_type").annotate(events=Count("id"))]
