from __future__ import annotations

import asyncio
import time
from typing import Any

# At module level: SQLAlchemy reads the models' Mapped[...] annotations from the module's names.
from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from orm_benchmark.constants import CATEGORIES, LOAD_CONCURRENCY, LOAD_OPERATIONS, POOL_MAX_SIZE
from orm_benchmark.declarations.databases import DATABASE_BY_KEY
from orm_benchmark.definitions.target import Target
from orm_benchmark.measuring.load_test import LoadTest
from orm_benchmark.measuring.timing import Timing
from orm_benchmark.measuring.workload import Workload


class SqlAlchemySuite:
    """The scenarios on SQLAlchemy's 2.0 async ORM - asyncpg on PostgreSQL, aiosqlite on SQLite - written
    the way its documentation writes them: a fresh session per timed repetition, ``select()`` with
    loader options, ORM bulk INSERT/UPDATE, the dialect's ``on_conflict_do_update()``.

    By default a session runs its statements in a transaction it commits (SQLAlchemy's own default).
    In the autocommit targets the engine commits every statement on its own - as hare and Django do -
    and the scenarios that need a transaction open a real one with the server's default isolation.
    """

    def __init__(self, target: Target, database: Any) -> None:
        """
        Args:
            target: The target.
            database: The run's ``PostgresqlDatabase`` or ``SqliteDatabase``.
        """
        self.target = target
        self.database = database
        self.is_sqlite = target.database == "sqlite"
        self.autocommit = "autocommit" in target.key
        self.excluded = DATABASE_BY_KEY[target.database].excluded_scenarios
        json_type = JSON if self.is_sqlite else JSONB

        class Base(AsyncAttrs, DeclarativeBase):
            pass

        widget_tags = Table(
            "widget_tag",
            Base.metadata,
            Column("widget_id", ForeignKey("widget.id", ondelete="CASCADE"), primary_key=True),
            Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
        )

        class SqlAlchemyWidget(Base):
            __tablename__ = "widget"

            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            name: Mapped[str] = mapped_column(String(100))
            category: Mapped[str] = mapped_column(String(20))
            value: Mapped[int] = mapped_column(Integer, default=0)
            score: Mapped[float] = mapped_column(Float, default=0.0)
            payload: Mapped[dict[str, Any]] = mapped_column(json_type, default=Workload.get_default_payload)
            gadgets: Mapped[list[SqlAlchemyGadget]] = relationship(back_populates="widget", passive_deletes=True)
            tags: Mapped[list[SqlAlchemyTag]] = relationship(
                secondary=widget_tags, back_populates="widgets", passive_deletes=True
            )

        class SqlAlchemyGadget(Base):
            __tablename__ = "gadget"

            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            widget_id: Mapped[int] = mapped_column(ForeignKey("widget.id", ondelete="CASCADE"), index=True)
            label: Mapped[str] = mapped_column(String(50))
            widget: Mapped[SqlAlchemyWidget] = relationship(back_populates="gadgets")

        class SqlAlchemyTag(Base):
            __tablename__ = "tag"

            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            name: Mapped[str] = mapped_column(String(50))
            widgets: Mapped[list[SqlAlchemyWidget]] = relationship(
                secondary=widget_tags, back_populates="tags", passive_deletes=True
            )

        self.base = Base
        self.widget_tags = widget_tags
        self.widget, self.gadget, self.tag = SqlAlchemyWidget, SqlAlchemyGadget, SqlAlchemyTag

    @staticmethod
    def make_rows(prefix: str, count: int, category_offset: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{prefix}-{index}",
                "category": CATEGORIES[(index + category_offset) % 10],
                "value": index,
                "score": index * 1.5,
                "payload": Workload.get_default_payload(),
            }
            for index in range(count)
        ]

    def create_engine(self) -> Any:
        """The engine of the run's database - on PostgreSQL with the shared pool size; on SQLite with
        SQLAlchemy's own pool of the file, which enforces its foreign keys only when asked."""
        from sqlalchemy import event
        from sqlalchemy.ext.asyncio import create_async_engine

        isolation = {"isolation_level": "AUTOCOMMIT"} if self.autocommit else {}
        if not self.is_sqlite:
            return create_async_engine(
                f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{self.database.port}/{self.database.name}",
                pool_size=POOL_MAX_SIZE,
                max_overflow=0,
                **isolation,
            )
        # Its connections wait up to 30 s, not sqlite3's 5 s, for the file's write lock - its pool of five
        # takes turns on one file, and the write load would otherwise stop at "database is locked".
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.database.path.as_posix()}", connect_args={"timeout": 30}, **isolation
        )

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(connection: Any, __: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    async def run(self, rows: int) -> dict[str, float | None]:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from sqlalchemy.orm import configure_mappers

        start = time.perf_counter()
        engine = self.create_engine()
        configure_mappers()
        async with engine.begin() as connection:
            await connection.run_sync(self.base.metadata.create_all)
        cold_start = (time.perf_counter() - start) * 1000
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        # In autocommit, Session.begin() sends no BEGIN - the transaction scenarios take their sessions
        # from the same pool with the server's default isolation instead.
        default_isolation = "SERIALIZABLE" if self.is_sqlite else "READ COMMITTED"
        transactional_factory = (
            async_sessionmaker(engine.execution_options(isolation_level=default_isolation), expire_on_commit=False)
            if self.autocommit
            else session_factory
        )

        async def touch() -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        await asyncio.gather(*[touch() for __ in range(POOL_MAX_SIZE)])
        try:
            results = await self.run_scenarios(session_factory, transactional_factory, rows)
            results["cold_start"] = cold_start
            results.update(await self.run_load_test(session_factory))
            results.update(await self.run_write_load_test(session_factory))
        finally:
            await engine.dispose()
        return results

    @staticmethod
    async def get_ids(session_factory: Any, model: Any, limit: int) -> list[int]:
        """The keys of a model's first rows.

        Args:
            session_factory: Makes the sessions.
            model: The model.
            limit: The most rows.

        Returns:
            The keys.
        """
        from sqlalchemy import select

        async with session_factory() as session:
            return list((await session.scalars(select(model.id).limit(limit))).all())

    @staticmethod
    async def run_and_commit(session_factory: Any, statement: Any, parameters: Any = None) -> None:
        """Runs a statement in a session of its own and commits it.

        Args:
            session_factory: Makes the sessions.
            statement: The statement.
            parameters: Its parameters.
        """
        async with session_factory() as session:
            await session.execute(statement, parameters)
            await session.commit()

    async def run_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int
    ) -> dict[str, float | None]:
        results: dict[str, float | None] = {}
        await self.run_insert_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_read_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_filter_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_relation_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_aggregate_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_update_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_delete_scenarios(session_factory, transactional_factory, rows, results)
        await self.run_transaction_scenarios(session_factory, transactional_factory, rows, results)
        return results

    async def run_insert_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The inserts - in bulk, of many rows and one by one."""
        from sqlalchemy import delete, insert, select

        widget_class = self.widget
        gadget_class = self.gadget
        tag_class = self.tag
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def clear_widgets() -> None:
            await self.run_and_commit(session_factory, delete(widget_class))

        async def bulk_create() -> None:
            await self.run_and_commit(session_factory, insert(widget_class), self.make_rows("widget", rows))

        results["bulk_create"] = await best(bulk_create, 3, clear_widgets)
        await clear_widgets()
        await self.run_and_commit(session_factory, insert(widget_class), self.make_rows("widget", rows))
        async with session_factory() as session:
            widgets = (await session.scalars(select(widget_class))).all()
            await session.execute(
                insert(gadget_class), [{"widget_id": w.id, "label": f"gadget-of-{w.name}"} for w in widgets]
            )
            tags = [tag_class(name=f"tag{index}") for index in range(5)]
            session.add_all(tags)
            await session.flush()
            for index, widget in enumerate(widgets):
                (await widget.awaitable_attrs.tags).append(tags[index % len(tags)])
            await session.commit()

        async def clear_large() -> None:
            await self.run_and_commit(
                session_factory, delete(widget_class).where(widget_class.name.startswith("large-"))
            )

        async def bulk_create_large() -> None:
            await self.run_and_commit(
                session_factory, insert(widget_class), self.make_rows("large", Workload.get_large_rows(rows))
            )

        results["bulk_create_large"] = await best(bulk_create_large, 3, clear_large)
        await clear_large()

        async def single_insert() -> None:
            async with session_factory() as session:
                for index in range(batch):
                    session.add(
                        widget_class(name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index)
                    )
                    await session.commit()

        results["single_insert"] = await best(single_insert, 3)

    async def run_read_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The reads of one model's rows - by key, the first, a page."""
        from sqlalchemy import delete, select

        widget_class = self.widget
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        await self.run_and_commit(session_factory, delete(widget_class).where(widget_class.name.startswith("single-")))

        async def fetch_all() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class))).all()

        results["fetch_all"] = await best(fetch_all, 5)
        ids = await self.get_ids(session_factory, widget_class, batch)

        async def get_by_pk() -> None:
            async with session_factory() as session:
                for primary_key in ids:
                    await session.get(widget_class, primary_key)

        results["get_by_pk"] = await best(get_by_pk, 3)

        async def first_row() -> None:
            async with session_factory() as session:
                for index in range(batch):
                    statement = (
                        select(widget_class)
                        .where(widget_class.category == CATEGORIES[index % 10])
                        .order_by(widget_class.id)
                        .limit(1)
                    )
                    (await session.scalars(statement)).first()

        results["first_row"] = await best(first_row, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            async with session_factory() as session:
                statement = select(widget_class).order_by(widget_class.id).offset(page_size * 2).limit(page_size)
                (await session.scalars(statement)).all()

        results["paginated_fetch"] = await best(paginated_fetch, 5)

        async def top_ten() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class).order_by(widget_class.value.desc()).limit(10))).all()

        results["top_ten"] = await best(top_ten, 5)

    async def run_filter_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The filtered reads of one model's rows."""
        from sqlalchemy import exists, or_, select
        from sqlalchemy.orm import load_only

        widget_class = self.widget
        gadget_class = self.gadget
        best = Timing.get_best

        async def filter_rows() -> None:
            async with session_factory() as session:
                statement = select(widget_class).where(
                    widget_class.category == CATEGORIES[0], widget_class.value >= rows // 4
                )
                (await session.scalars(statement)).all()

        results["filter"] = await best(filter_rows, 5)

        async def icontains_search() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class).where(widget_class.name.icontains("GET-1")))).all()

        results["icontains_search"] = await best(icontains_search, 5)

        async def exists_check() -> None:
            async with session_factory() as session:
                await session.scalar(select(exists().where(widget_class.category == CATEGORIES[0])))

        results["exists_check"] = await best(exists_check, 5)

        async def values_list_flat() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class.id).where(widget_class.value >= rows // 4))).all()

        results["values_list_flat"] = await best(values_list_flat, 5)

        async def distinct_values() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class.category).distinct())).all()

        results["distinct_values"] = await best(distinct_values, 5)

        async def only_fields() -> None:
            async with session_factory() as session:
                statement = (
                    select(widget_class)
                    .options(load_only(widget_class.id, widget_class.name))
                    .where(widget_class.value >= rows // 4)
                )
                (await session.scalars(statement)).all()

        results["only_fields"] = await best(only_fields, 5)

        async def complex_filter() -> None:
            async with session_factory() as session:
                statement = (
                    select(widget_class)
                    .join(widget_class.gadgets)
                    .where(
                        or_(widget_class.category == CATEGORIES[0], widget_class.category == CATEGORIES[1]),
                        widget_class.value >= rows // 4,
                        gadget_class.label.startswith("gadget-of-widget"),
                    )
                    .order_by(widget_class.value)
                    .distinct()
                )
                (await session.scalars(statement)).all()

        results["complex_filter"] = await best(complex_filter, 5)
        large_in_ids = await self.get_ids(session_factory, widget_class, Workload.get_in_count(rows))

        async def large_in_filter() -> None:
            async with session_factory() as session:
                statement = select(widget_class).where(widget_class.id.in_(large_in_ids)).order_by(widget_class.id)
                (await session.scalars(statement)).all()

        results["large_in_filter"] = await best(large_in_filter, 5)

    async def run_relation_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The reads across relations."""
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload, selectinload

        widget_class = self.widget
        gadget_class = self.gadget
        tag_class = self.tag
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def iterate_chunks() -> None:
            async with transactional_factory() as session:
                statement = select(widget_class).order_by(widget_class.id).execution_options(yield_per=200)
                async for widget in await session.stream_scalars(statement):
                    widget.name  # noqa: B018 - read, as a caller would

        results["iterate_chunks"] = await best(iterate_chunks, 5)
        prefetch_ids = await self.get_ids(session_factory, widget_class, batch)

        async def prefetch_related() -> None:
            async with session_factory() as session:
                statement = (
                    select(widget_class)
                    .where(widget_class.id.in_(prefetch_ids))
                    .options(selectinload(widget_class.gadgets))
                )
                for widget in (await session.scalars(statement)).all():
                    list(widget.gadgets)

        results["prefetch_related"] = await best(prefetch_related, 5)

        async def prefetch_many_to_many() -> None:
            async with session_factory() as session:
                statement = (
                    select(widget_class)
                    .where(widget_class.id.in_(prefetch_ids))
                    .options(selectinload(widget_class.tags))
                )
                for widget in (await session.scalars(statement)).all():
                    list(widget.tags)

        results["prefetch_many_to_many"] = await best(prefetch_many_to_many, 5)

        async def n_plus_one() -> None:
            async with session_factory() as session:
                for widget in (
                    await session.scalars(select(widget_class).where(widget_class.id.in_(prefetch_ids)))
                ).all():
                    await widget.awaitable_attrs.gadgets

        results["n_plus_one"] = await best(n_plus_one, 3)
        gadget_ids = await self.get_ids(session_factory, gadget_class, batch)

        async def select_related() -> None:
            async with session_factory() as session:
                statement = (
                    select(gadget_class)
                    .where(gadget_class.id.in_(gadget_ids))
                    .options(joinedload(gadget_class.widget))
                )
                for gadget in (await session.scalars(statement)).all():
                    gadget.widget.name  # noqa: B018 - already joined

        results["select_related_join"] = await best(select_related, 5)

        async def related_filter() -> None:
            async with session_factory() as session:
                statement = (
                    select(gadget_class).join(gadget_class.widget).where(widget_class.category == CATEGORIES[0])
                )
                (await session.scalars(statement)).all()

        results["related_filter"] = await best(related_filter, 5)

        async def many_to_many_filter() -> None:
            async with session_factory() as session:
                statement = select(widget_class).join(widget_class.tags).where(tag_class.name == "tag1")
                (await session.scalars(statement)).all()

        results["many_to_many_filter"] = await best(many_to_many_filter, 5)

    async def run_aggregate_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The annotations and the aggregates."""
        from sqlalchemy import case, exists, func, select

        widget_class = self.widget
        gadget_class = self.gadget
        tag_class = self.tag
        best = Timing.get_best

        async def annotate_count() -> None:
            async with session_factory() as session:
                statement = (
                    select(widget_class.id, func.count(tag_class.id).label("tag_count"))
                    .outerjoin(widget_class.tags)
                    .group_by(widget_class.id)
                )
                (await session.execute(statement)).all()

        results["annotate_count"] = await best(annotate_count, 5)

        async def exists_annotation() -> None:
            async with session_factory() as session:
                has_gadget = exists().where(gadget_class.widget_id == widget_class.id).label("has_gadget")
                (await session.execute(select(widget_class.id, has_gadget))).all()

        results["exists_annotation"] = await best(exists_annotation, 5)

        async def count() -> None:
            async with session_factory() as session:
                await session.scalar(
                    select(func.count()).select_from(widget_class).where(widget_class.value >= rows // 2)
                )

        results["count"] = await best(count, 5)

        async def aggregate() -> None:
            async with session_factory() as session:
                statement = select(
                    func.sum(widget_class.value),
                    func.avg(widget_class.score),
                    func.max(widget_class.value),
                    func.min(widget_class.value),
                )
                (await session.execute(statement)).one()

        results["aggregate"] = await best(aggregate, 5)

        async def conditional_count() -> None:
            async with session_factory() as session:
                await session.scalar(select(func.count(widget_class.id).filter(widget_class.value >= rows // 2)))

        results["conditional_count"] = await best(conditional_count, 5)

        async def group_by() -> None:
            async with session_factory() as session:
                statement = select(
                    widget_class.category, func.count(widget_class.id), func.sum(widget_class.value)
                ).group_by(widget_class.category)
                (await session.execute(statement)).all()

        results["group_by"] = await best(group_by, 5)
        having_threshold = rows * rows // 20

        async def having() -> None:
            async with session_factory() as session:
                total = func.sum(widget_class.value)
                statement = (
                    select(widget_class.category, total.label("total"))
                    .group_by(widget_class.category)
                    .having(total > having_threshold)
                )
                (await session.execute(statement)).all()

        results["having"] = await best(having, 5)

        async def annotate_values() -> None:
            async with session_factory() as session:
                statement = select(
                    widget_class.id,
                    widget_class.name,
                    widget_class.value,
                    func.coalesce(widget_class.value, 0).label("doubled"),
                )
                (await session.execute(statement)).all()

        results["annotate_values"] = await best(annotate_values, 5)

        async def case_when() -> None:
            async with session_factory() as session:
                bucket = case((widget_class.value >= rows // 2, "high"), else_="low").label("bucket")
                (await session.execute(select(widget_class.id, bucket))).all()

        results["case_when_conditional"] = await best(case_when, 5)

        async def window_rank() -> None:
            async with session_factory() as session:
                rank = (
                    func.rank()
                    .over(partition_by=widget_class.category, order_by=widget_class.value.desc())
                    .label("rank")
                )
                (await session.execute(select(widget_class.id, rank))).all()

        results["window_rank"] = await best(window_rank, 5)

    async def run_update_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The updates and the upserts."""
        from sqlalchemy import delete, select, update
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        widget_class = self.widget
        tag_class = self.tag
        dialect_insert = sqlite_insert if self.is_sqlite else postgresql_insert
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def update_bulk() -> None:
            await self.run_and_commit(
                session_factory, update(widget_class).where(widget_class.category == CATEGORIES[1]).values(value=0)
            )

        results["update_bulk"] = await best(update_bulk, 3)

        async def update_expression() -> None:
            await self.run_and_commit(
                session_factory,
                update(widget_class)
                .where(widget_class.category == CATEGORIES[4])
                .values(value=widget_class.value + 1),
            )

        results["update_expression"] = await best(update_expression, 3)
        loop_ids = await self.get_ids(session_factory, widget_class, batch)

        async def update_by_pk() -> None:
            async with session_factory() as session:
                for primary_key in loop_ids:
                    await session.execute(
                        update(widget_class).where(widget_class.id == primary_key).values(value=primary_key)
                    )
                    await session.commit()

        results["update_by_pk"] = await best(update_by_pk, 3)

        async def update_loop() -> None:
            async with session_factory() as session:
                for primary_key in loop_ids:
                    widget = await session.get(widget_class, primary_key)
                    widget.value = widget.value + 1
                    await session.commit()

        results["update_loop"] = await best(update_loop, 3)

        async def bulk_update() -> None:
            async with session_factory() as session:
                widgets_to_update = (await session.scalars(select(widget_class).limit(batch))).all()
                await session.execute(
                    update(widget_class), [{"id": w.id, "value": w.value + 100} for w in widgets_to_update]
                )
                await session.commit()

        results["bulk_update"] = await best(bulk_update, 3)
        async with session_factory() as many_to_many_session:
            many_to_many_widgets = (await many_to_many_session.scalars(select(widget_class).limit(batch))).all()
            extra_tag = tag_class(name="tag_extra")
            many_to_many_session.add(extra_tag)
            await many_to_many_session.commit()

            async def many_to_many_add() -> None:
                for widget in many_to_many_widgets:
                    (await widget.awaitable_attrs.tags).append(extra_tag)
                    await many_to_many_session.commit()

            results["many_to_many_add"] = await best(many_to_many_add, 1)
            await many_to_many_session.execute(
                delete(self.widget_tags).where(self.widget_tags.c.tag_id == extra_tag.id)
            )
            await many_to_many_session.commit()
        async with session_factory() as session:
            upsert_widgets = (await session.scalars(select(widget_class).limit(batch))).all()

        async def upsert() -> None:
            statement = dialect_insert(widget_class).values(
                [
                    {"id": w.id, "name": w.name, "category": w.category, "value": w.value + 1, "score": w.score}
                    for w in upsert_widgets
                ]
            )
            statement = statement.on_conflict_do_update(
                index_elements=[widget_class.id], set_={"value": statement.excluded.value}
            )
            await self.run_and_commit(session_factory, statement)

        results["upsert"] = await best(upsert, 3)

    async def run_delete_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """get_or_create() and the deletes."""
        from sqlalchemy import delete, insert, select

        widget_class = self.widget
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def clear_get_or_create() -> None:
            await self.run_and_commit(
                session_factory, delete(widget_class).where(widget_class.name.startswith("goc-"))
            )

        async def get_or_create() -> None:
            async with session_factory() as session:
                for index in range(batch):
                    existing = await session.scalar(select(widget_class).where(widget_class.name == f"goc-{index}"))
                    if existing is None:
                        session.add(
                            widget_class(
                                name=f"goc-{index}", category=CATEGORIES[index % 10], value=index, score=index
                            )
                        )
                        await session.commit()

        results["get_or_create"] = await best(get_or_create, 3, clear_get_or_create)
        await clear_get_or_create()

        async def delete_bulk() -> None:
            await self.run_and_commit(
                session_factory, delete(widget_class).where(widget_class.category == CATEGORIES[2])
            )

        results["delete_bulk"] = await best(delete_bulk, 1)
        await self.run_and_commit(
            session_factory, insert(widget_class), self.make_rows("refill", rows // 10 + 1, category_offset=2)
        )
        delete_ids = await self.get_ids(session_factory, widget_class, batch)

        async def delete_loop() -> None:
            async with session_factory() as session:
                for primary_key in delete_ids:
                    await session.delete(await session.get(widget_class, primary_key))
                    await session.commit()

        results["delete_loop"] = await best(delete_loop, 1)

    async def run_transaction_scenarios(
        self, session_factory: Any, transactional_factory: Any, rows: int, results: dict[str, float | None]
    ) -> None:
        """The concurrent reads, the transactions and the JSON values."""
        from sqlalchemy import insert, select, update

        widget_class = self.widget
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        await self.run_and_commit(session_factory, insert(widget_class), self.make_rows("widget", batch))
        concurrent_ids = await self.get_ids(session_factory, widget_class, batch)

        async def get_in_own_session(primary_key: int) -> None:
            async with session_factory() as session:
                await session.get(widget_class, primary_key)

        async def concurrent_get() -> None:
            await asyncio.gather(*[get_in_own_session(primary_key) for primary_key in concurrent_ids])

        results["concurrent_get"] = await best(concurrent_get, 3)
        atomic_ids = await self.get_ids(session_factory, widget_class, batch)

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                async with transactional_factory.begin() as session:
                    widget = await session.get(widget_class, primary_key)
                    widget.value = widget.value + 1

        results["atomic_update_loop"] = await best(atomic_update_loop, 3)

        async def nested_transaction_loop() -> None:
            async with transactional_factory.begin() as session:
                for primary_key in atomic_ids:
                    async with session.begin_nested():
                        widget = await session.get(widget_class, primary_key)
                        widget.value = widget.value + 1

        results["nested_transaction_loop"] = await best(nested_transaction_loop, 3)

        async def select_for_update_loop() -> None:
            for primary_key in atomic_ids:
                async with transactional_factory.begin() as session:
                    statement = select(widget_class).where(widget_class.id == primary_key).with_for_update()
                    widget = (await session.scalars(statement)).one()
                    widget.value = widget.value + 1

        if "select_for_update_loop" not in self.excluded:
            results["select_for_update_loop"] = await best(select_for_update_loop, 3)

        async def json_write() -> None:
            await self.run_and_commit(
                session_factory,
                update(widget_class)
                .where(widget_class.category == CATEGORIES[3])
                .values(payload={"updated": True, "items": list(range(20))}),
            )

        results["json_write"] = await best(json_write, 5)

        async def json_read() -> None:
            async with session_factory() as session:
                for widget in (await session.scalars(select(widget_class))).all():
                    len(widget.payload)

        results["json_read"] = await best(json_read, 5)

    async def get_load_ids(self, session_factory: Any) -> list[int]:
        from sqlalchemy import select

        async with session_factory() as session:
            return list((await session.scalars(select(self.widget.id).limit(200))).all())

    async def run_load_test(self, session_factory: Any) -> dict[str, float]:
        from sqlalchemy import select

        widget_class, ids = self.widget, await self.get_load_ids(session_factory)

        async def operation(index: int) -> None:
            operation_slot = index % 20
            async with session_factory() as session:
                if operation_slot < 16:
                    await session.get(widget_class, ids[index % len(ids)])
                elif operation_slot < 19:
                    statement = select(widget_class).where(widget_class.category == CATEGORIES[index % 10]).limit(10)
                    (await session.scalars(statement)).all()
                else:
                    widget = await session.get(widget_class, ids[index % len(ids)])
                    widget.value = widget.value + 1
                    await session.commit()

        return await LoadTest.run("load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)

    async def run_write_load_test(self, session_factory: Any) -> dict[str, float]:
        widget_class, ids = self.widget, await self.get_load_ids(session_factory)

        async def operation(index: int) -> None:
            async with session_factory() as session:
                widget = await session.get(widget_class, ids[index % len(ids)])
                if index % 2:
                    widget.value = widget.value + 1
                    await session.commit()

        return await LoadTest.run("write_load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)
