from __future__ import annotations

import datetime
import uuid
from typing import Any

from orm_benchmark.suites.clickhouse.clickhouse_suite import ClickhouseSuite


class ClickhouseConnectSuite(ClickhouseSuite):
    """The scenarios on clickhouse-connect's asyncio client alone, without an ORM: SQL text with bound
    parameters and its column-wise ``insert()`` - the floor an ORM over the same driver builds on."""

    COLUMNS = ("id", "site", "user_id", "amount", "happened_at", "event_type")

    def load(self) -> None:
        import clickhouse_connect  # noqa: F401

    async def connect(self) -> None:
        import clickhouse_connect

        # A mutation waits for the rows it changes, as hare's does.
        self.client = await clickhouse_connect.get_async_client(
            host="127.0.0.1",
            port=self.database.http_port,
            username="default",
            password=self.database.password,
            database=self.database.name,
            settings={"mutations_sync": 1},
        )
        await self.client.query("SELECT 1")

    async def close(self) -> None:
        await self.client.close()

    async def create_table(self) -> None:
        await self.client.command(
            "CREATE TABLE event (id UUID, site String, user_id Int32, amount Float64, "
            "happened_at DateTime64(6, 'UTC'), event_type String) ENGINE = MergeTree ORDER BY id"
        )

    async def insert(self, rows: list[dict[str, Any]]) -> None:
        await self.client.insert(
            "event", [[row[column] for column in self.COLUMNS] for row in rows], column_names=self.COLUMNS
        )

    async def count_filter(self) -> None:
        await self.client.query(
            "SELECT count() FROM event WHERE event_type = {event_type:String} AND amount > {amount:Float64}",
            parameters={"event_type": "buy", "amount": 50},
        )

    async def group_sum(self) -> None:
        (await self.client.query("SELECT site, sum(amount), avg(amount) FROM event GROUP BY site")).result_rows

    async def top_n(self) -> None:
        (
            await self.client.query(
                "SELECT user_id, sum(amount) AS total FROM event GROUP BY user_id ORDER BY total DESC LIMIT 10"
            )
        ).result_rows

    async def by_day(self) -> None:
        (
            await self.client.query(
                "SELECT toStartOfDay(happened_at) AS day, count() FROM event GROUP BY day ORDER BY day"
            )
        ).result_rows

    async def distinct_count(self) -> None:
        await self.client.query("SELECT count(DISTINCT user_id) FROM event")

    async def page_values(self) -> None:
        (
            await self.client.query("SELECT id, site, amount FROM event ORDER BY id LIMIT 100 OFFSET 1000")
        ).named_results()

    async def get_by_key(self, key: uuid.UUID) -> None:
        result = await self.client.query("SELECT * FROM event WHERE id = {id:UUID}", parameters={"id": key})
        list(result.named_results())

    async def update_mutation(self) -> None:
        await self.client.command("ALTER TABLE event UPDATE amount = 0 WHERE event_type = 'share'")

    async def delete_mutation(self, site: str) -> None:
        await self.client.command("ALTER TABLE event DELETE WHERE site = {site:String}", parameters={"site": site})

    async def count_day(self, day: datetime.datetime) -> None:
        (
            await self.client.query(
                "SELECT event_type, count() FROM event WHERE happened_at >= {start:DateTime64(6)} "
                "AND happened_at < {end:DateTime64(6)} GROUP BY event_type",
                parameters={"start": day, "end": day + datetime.timedelta(days=1)},
            )
        ).result_rows
