from __future__ import annotations

import datetime
import uuid
from typing import Any

from orm_benchmark.suites.clickhouse.clickhouse_suite import ClickhouseSuite


class SqlAlchemyClickhouseSuite(ClickhouseSuite):
    """The scenarios on SQLAlchemy's async ORM with clickhouse-sqlalchemy's dialect over the asynch
    driver (ClickHouse's native protocol) - in a virtual environment of its own, as clickhouse-sqlalchemy
    needs SQLAlchemy 2.0. ``update()`` and ``delete()`` compile to ClickHouse's mutations. ClickHouse has no
    transactions and asynch refuses ``COMMIT``: a statement runs on its own, nothing is committed."""

    def load(self) -> None:
        from clickhouse_sqlalchemy import engines, get_declarative_base, types
        from sqlalchemy import Column
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: F401

        base = get_declarative_base()

        class SqlAlchemyEvent(base):
            __tablename__ = "event"

            id = Column(types.UUID, primary_key=True)
            site = Column(types.String)
            user_id = Column(types.Int32)
            amount = Column(types.Float64)
            happened_at = Column(types.DateTime64(6, timezone="UTC"))
            event_type = Column(types.String)

            __table_args__ = (engines.MergeTree(order_by="id"),)

        self.base, self.event_class = base, SqlAlchemyEvent

    async def connect(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        database = self.database
        # A mutation waits for the rows it changes, as hare's does.
        self.engine = create_async_engine(
            f"clickhouse+asynch://default:{database.password}@127.0.0.1:{database.native_port}/{database.name}",
            connect_args={"settings": {"mutations_sync": 1}},
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_table(self) -> None:
        async with self.engine.connect() as connection:
            await connection.run_sync(self.base.metadata.create_all)

    async def insert(self, rows: list[dict[str, Any]]) -> None:
        from sqlalchemy import insert

        async with self.session_factory() as session:
            # The table's insert, as clickhouse-sqlalchemy writes it - the ORM's bulk path reads RETURNING.
            await session.execute(insert(self.event_class.__table__), rows)

    async def count_filter(self) -> None:
        from sqlalchemy import func, select

        event = self.event_class
        async with self.session_factory() as session:
            await session.scalar(
                select(func.count()).select_from(event).where(event.event_type == "buy", event.amount > 50)
            )

    async def group_sum(self) -> None:
        from sqlalchemy import func, select

        event = self.event_class
        async with self.session_factory() as session:
            (
                await session.execute(
                    select(event.site, func.sum(event.amount), func.avg(event.amount)).group_by(event.site)
                )
            ).all()

    async def top_n(self) -> None:
        from sqlalchemy import func, select

        event = self.event_class
        async with self.session_factory() as session:
            total = func.sum(event.amount).label("total")
            statement = select(event.user_id, total).group_by(event.user_id).order_by(total.desc()).limit(10)
            (await session.execute(statement)).all()

    async def by_day(self) -> None:
        from sqlalchemy import func, select

        event = self.event_class
        async with self.session_factory() as session:
            day = func.toStartOfDay(event.happened_at).label("day")
            (await session.execute(select(day, func.count()).group_by(day).order_by(day))).all()

    async def distinct_count(self) -> None:
        from sqlalchemy import distinct, func, select

        event = self.event_class
        async with self.session_factory() as session:
            await session.scalar(select(func.count(distinct(event.user_id))))

    async def page_values(self) -> None:
        from sqlalchemy import select

        event = self.event_class
        async with self.session_factory() as session:
            statement = select(event.id, event.site, event.amount).order_by(event.id).offset(1000).limit(100)
            (await session.execute(statement)).all()

    async def get_by_key(self, key: uuid.UUID) -> None:
        async with self.session_factory() as session:
            await session.get(self.event_class, key)

    async def update_mutation(self) -> None:
        from sqlalchemy import update

        event = self.event_class
        async with self.session_factory() as session:
            # The table's mutation, as clickhouse-sqlalchemy writes it - the ORM's bulk path reads RETURNING.
            table = event.__table__
            await session.execute(update(table).where(table.c.event_type == "share").values(amount=0))

    async def delete_mutation(self, site: str) -> None:
        from sqlalchemy import delete

        event = self.event_class
        async with self.session_factory() as session:
            table = event.__table__
            await session.execute(delete(table).where(table.c.site == site))

    async def count_day(self, day: datetime.datetime) -> None:
        from sqlalchemy import func, select

        event = self.event_class
        async with self.session_factory() as session:
            statement = (
                select(event.event_type, func.count())
                .where(event.happened_at >= day, event.happened_at < day + datetime.timedelta(days=1))
                .group_by(event.event_type)
            )
            (await session.execute(statement)).all()
