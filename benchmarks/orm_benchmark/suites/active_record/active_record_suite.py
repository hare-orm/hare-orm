from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

from orm_benchmark.constants import CATEGORIES, LOAD_CONCURRENCY, LOAD_OPERATIONS, POOL_MAX_SIZE
from orm_benchmark.declarations.databases import DATABASE_BY_KEY
from orm_benchmark.definitions.target import Target
from orm_benchmark.measuring.load_test import LoadTest
from orm_benchmark.measuring.timing import Timing
from orm_benchmark.measuring.workload import Workload
from orm_benchmark.suites.active_record.active_record_orm import ActiveRecordOrm


class ActiveRecordSuite:
    """The scenarios on hare, tortoise-orm or yara-orm, on PostgreSQL or SQLite."""

    def __init__(self, target: Target, database: Any) -> None:
        """
        Args:
            target: The target.
            database: The run's ``PostgresqlDatabase`` or ``SqliteDatabase``.
        """
        self.target = target
        self.database = database
        self.excluded = DATABASE_BY_KEY[target.database].excluded_scenarios
        self.orm = ActiveRecordOrm(target)
        ActiveRecordOrm.current = self.orm
        models = importlib.import_module("orm_benchmark.suites.active_record.active_record_models")
        self.widget_class, self.gadget_class, self.tag_class = models.Widget, models.Gadget, models.Tag
        self.widgets = self.orm.get_queries(models.Widget)
        self.gadgets = self.orm.get_queries(models.Gadget)
        self.tags = self.orm.get_queries(models.Tag)

    def make_widgets(self, prefix: str, count: int, category_offset: int = 0) -> list[Any]:
        return [
            self.widget_class(
                name=f"{prefix}-{index}",
                category=CATEGORIES[(index + category_offset) % 10],
                value=index,
                score=index * 1.5,
            )
            for index in range(count)
        ]

    async def seed_relations(self) -> None:
        """One gadget per widget, and five tags linked in turn."""
        widgets = await self.widgets.all()
        await self.gadgets.bulk_create(
            [self.gadget_class(widget=widget, label=f"gadget-of-{widget.name}") for widget in widgets]
        )
        tags = [await self.tags.create(name=f"tag{index}") for index in range(5)]
        for index, widget in enumerate(widgets):
            await widget.tags.add(tags[index % len(tags)])

    async def run(self, rows: int) -> dict[str, float | None]:
        """Runs every scenario and both load tests.

        Args:
            rows: The table size.

        Returns:
            Each scenario's time in milliseconds - None for one the ORM has no way to write - and the
            load tests' figures.
        """
        module_name = "orm_benchmark.suites.active_record.active_record_models"
        start = time.perf_counter()
        await self.orm.init_class.init(
            config={
                "connections": {"default": self.orm.get_db_url(self.database)},
                "apps": {"models": {"models": [module_name], "default_connection": "default"}},
            }
        )
        cold_start = (time.perf_counter() - start) * 1000
        await self.orm.init_class.generate_schemas()
        await asyncio.gather(*[self.widgets.all().limit(1) for __ in range(POOL_MAX_SIZE)])
        try:
            results = await self.run_scenarios(rows)
            results["cold_start"] = cold_start
            results.update(await self.run_load_test())
            results.update(await self.run_write_load_test())
        finally:
            await self.orm.init_class.close_connections()
        return results

    async def run_scenarios(self, rows: int) -> dict[str, float | None]:
        results: dict[str, float | None] = {}
        await self.run_insert_scenarios(rows, results)
        await self.run_read_scenarios(rows, results)
        await self.run_filter_scenarios(rows, results)
        await self.run_relation_scenarios(rows, results)
        await self.run_aggregate_scenarios(rows, results)
        await self.run_update_scenarios(rows, results)
        await self.run_delete_scenarios(rows, results)
        await self.run_transaction_scenarios(rows, results)
        return results

    async def run_insert_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The inserts - in bulk, of many rows and one by one."""
        widgets = self.widgets
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def clear_widgets() -> None:
            await widgets.all().delete()

        async def bulk_create() -> None:
            await widgets.bulk_create(self.make_widgets("widget", rows))

        results["bulk_create"] = await best(bulk_create, 3, clear_widgets)
        await clear_widgets()
        await widgets.bulk_create(self.make_widgets("widget", rows))
        await self.seed_relations()

        async def clear_large() -> None:
            await widgets.filter(name__startswith="large-").delete()

        async def bulk_create_large() -> None:
            await widgets.bulk_create(self.make_widgets("large", Workload.get_large_rows(rows)))

        results["bulk_create_large"] = await best(bulk_create_large, 3, clear_large)
        await clear_large()

        async def single_insert() -> None:
            for index in range(batch):
                await widgets.create(name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index)

        results["single_insert"] = await best(single_insert, 3)

    async def run_read_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The reads of one model's rows - by key, the first, a page."""
        widgets = self.widgets
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        await widgets.filter(name__startswith="single-").delete()

        async def fetch_all() -> None:
            await widgets.all()

        results["fetch_all"] = await best(fetch_all, 5)
        ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def get_by_pk() -> None:
            for primary_key in ids:
                await widgets.get(id=primary_key)

        results["get_by_pk"] = await best(get_by_pk, 3)

        async def first_row() -> None:
            for index in range(batch):
                await widgets.filter(category=CATEGORIES[index % 10]).order_by("id").first()

        results["first_row"] = await best(first_row, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            await widgets.all().order_by("id").limit(page_size).offset(page_size * 2)

        results["paginated_fetch"] = await best(paginated_fetch, 5)

        async def top_ten() -> None:
            await widgets.all().order_by("-value").limit(10)

        results["top_ten"] = await best(top_ten, 5)

    async def run_filter_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The filtered reads of one model's rows."""
        orm = self.orm
        widgets = self.widgets
        best = Timing.get_best

        async def filter_rows() -> None:
            await widgets.filter(category=CATEGORIES[0], value__gte=rows // 4)

        results["filter"] = await best(filter_rows, 5)

        async def icontains_search() -> None:
            await widgets.filter(name__icontains="GET-1")

        results["icontains_search"] = await best(icontains_search, 5)

        async def exists_check() -> None:
            await widgets.filter(category=CATEGORIES[0]).exists()

        results["exists_check"] = await best(exists_check, 5)

        async def values_list_flat() -> None:
            await widgets.filter(value__gte=rows // 4).values_list("id", flat=True)

        results["values_list_flat"] = await best(values_list_flat, 5)

        async def distinct_values() -> None:
            await widgets.all().distinct().values_list("category", flat=True)

        results["distinct_values"] = await best(distinct_values, 5)

        async def only_fields() -> None:
            await widgets.filter(value__gte=rows // 4).only("id", "name")

        results["only_fields"] = await best(only_fields, 5)

        async def complex_filter() -> None:
            await (
                widgets.filter(
                    orm.q(category=CATEGORIES[0]) | orm.q(category=CATEGORIES[1]),
                    value__gte=rows // 4,
                    gadgets__label__startswith="gadget-of-widget",
                )
                .order_by("value")
                .distinct()
            )

        results["complex_filter"] = await best(complex_filter, 5)
        large_in_ids = [widget.id for widget in await widgets.all().limit(Workload.get_in_count(rows))]

        async def large_in_filter() -> None:
            await widgets.filter(id__in=large_in_ids).order_by("id")

        results["large_in_filter"] = await best(large_in_filter, 5)

    async def run_relation_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The reads across relations."""
        orm = self.orm
        widgets = self.widgets
        gadgets = self.gadgets
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def iterate_chunks() -> None:
            async for widget in widgets.all().order_by("id").iterator(chunk_size=200):
                widget.name  # noqa: B018 - read, as a caller would

        results["iterate_chunks"] = await best(iterate_chunks, 5) if orm.supports_chunked_iteration else None
        prefetch_ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def prefetch_related() -> None:
            for widget in await widgets.filter(id__in=prefetch_ids).prefetch_related("gadgets"):
                [gadget async for gadget in widget.gadgets]

        results["prefetch_related"] = await best(prefetch_related, 5)

        async def prefetch_many_to_many() -> None:
            for widget in await widgets.filter(id__in=prefetch_ids).prefetch_related("tags"):
                [tag async for tag in widget.tags]

        results["prefetch_many_to_many"] = await best(prefetch_many_to_many, 5)

        async def n_plus_one() -> None:
            for widget in await widgets.filter(id__in=prefetch_ids):
                await widget.gadgets.all()

        results["n_plus_one"] = await best(n_plus_one, 3)
        gadget_ids = [gadget.id for gadget in await gadgets.all().limit(batch)]

        async def select_related() -> None:
            for gadget in await gadgets.filter(id__in=gadget_ids).select_related("widget"):
                gadget.widget.name  # noqa: B018 - already joined

        results["select_related_join"] = await best(select_related, 5)

        async def related_filter() -> None:
            await gadgets.filter(widget__category=CATEGORIES[0])

        results["related_filter"] = await best(related_filter, 5)

        async def many_to_many_filter() -> None:
            await widgets.filter(tags__name="tag1")

        results["many_to_many_filter"] = await best(many_to_many_filter, 5)

    async def run_aggregate_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The annotations and the aggregates."""
        orm = self.orm
        functions = orm.functions
        widgets = self.widgets
        gadgets = self.gadgets
        best = Timing.get_best

        async def annotate_count() -> None:
            await widgets.annotate(tag_count=functions.Count("tags")).values("id", "tag_count")

        results["annotate_count"] = await best(annotate_count, 5)

        async def exists_annotation() -> None:
            has_gadget = orm.exists(gadgets.filter(widget=orm.outer_reference("pk")))
            await widgets.annotate(has_gadget=has_gadget).values("id", "has_gadget")

        results["exists_annotation"] = await best(exists_annotation, 5) if orm.supports_exists else None

        async def count() -> None:
            await widgets.filter(value__gte=rows // 2).count()

        results["count"] = await best(count, 5)

        async def aggregate() -> None:
            aggregates = {
                "total": functions.Sum("value"),
                "avg_score": functions.Avg("score"),
                "hi": functions.Max("value"),
                "lo": functions.Min("value"),
            }
            if orm.is_hare:
                # hare follows Django: the one-row total is QuerySet.aggregate().
                await widgets.all().aggregate(**aggregates)
            else:
                await widgets.annotate(**aggregates).values("total", "avg_score", "hi", "lo")

        results["aggregate"] = await best(aggregate, 5)

        async def conditional_count() -> None:
            high = functions.Count("id", _filter=orm.q(value__gte=rows // 2))
            if orm.is_hare:
                await widgets.all().aggregate(high=high)
            else:
                await widgets.annotate(high=high).values("high")

        results["conditional_count"] = await best(conditional_count, 5)

        async def group_by() -> None:
            await (
                widgets.annotate(cnt=functions.Count("id"), total=functions.Sum("value"))
                .group_by("category")
                .values("category", "cnt", "total")
            )

        results["group_by"] = await best(group_by, 5)
        having_threshold = rows * rows // 20

        async def having() -> None:
            await (
                widgets.annotate(total=functions.Sum("value"))
                .group_by("category")
                .filter(total__gt=having_threshold)
                .values("category", "total")
            )

        results["having"] = await best(having, 5)

        async def annotate_values() -> None:
            await widgets.annotate(doubled=orm.coalesce("value", 0)).values("id", "name", "value", "doubled")

        results["annotate_values"] = await best(annotate_values, 5)

        async def case_when() -> None:
            await widgets.annotate(bucket=orm.case(orm.when(value__gte=rows // 2, then="high"), default="low")).values(
                "id", "bucket"
            )

        results["case_when_conditional"] = await best(case_when, 5)

        async def window_rank() -> None:
            rank = orm.window(orm.rank(), partition_by=["category"], order_by=["-value"])
            await widgets.annotate(rank=rank).values("id", "rank")

        results["window_rank"] = await best(window_rank, 5) if orm.supports_window else None

    async def run_update_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The updates and the upserts."""
        orm = self.orm
        widgets = self.widgets
        tags = self.tags
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def update_bulk() -> None:
            await widgets.filter(category=CATEGORIES[1]).update(value=0)

        results["update_bulk"] = await best(update_bulk, 3)

        async def update_expression() -> None:
            await widgets.filter(category=CATEGORIES[4]).update(value=orm.f("value") + 1)

        results["update_expression"] = await best(update_expression, 3)
        loop_ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def update_by_pk() -> None:
            for primary_key in loop_ids:
                await widgets.filter(id=primary_key).update(value=primary_key)

        results["update_by_pk"] = await best(update_by_pk, 3)

        async def update_loop() -> None:
            for primary_key in loop_ids:
                widget = await widgets.get(id=primary_key)
                widget.value = widget.value + 1
                await widget.save(update_fields=["value"])

        results["update_loop"] = await best(update_loop, 3)

        async def bulk_update() -> None:
            widgets_to_update = await widgets.all().limit(batch)
            for widget in widgets_to_update:
                widget.value = widget.value + 100
            await widgets.bulk_update(widgets_to_update, fields=["value"])

        results["bulk_update"] = await best(bulk_update, 3)
        many_to_many_widgets = await widgets.all().limit(batch)
        extra_tag = await tags.create(name="tag_extra")

        async def many_to_many_add() -> None:
            for widget in many_to_many_widgets:
                await widget.tags.add(extra_tag)

        results["many_to_many_add"] = await best(many_to_many_add, 1)
        await extra_tag.widgets.clear()
        upsert_widgets = await widgets.all().limit(batch)

        async def upsert() -> None:
            await widgets.bulk_create(
                [
                    self.widget_class(
                        id=widget.id,
                        name=widget.name,
                        category=widget.category,
                        value=widget.value + 1,
                        score=widget.score,
                    )
                    for widget in upsert_widgets
                ],
                on_conflict=["id"],
                update_fields=["value"],
            )

        results["upsert"] = await best(upsert, 3)

    async def run_delete_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """get_or_create() and the deletes."""
        widgets = self.widgets
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def clear_get_or_create() -> None:
            await widgets.filter(name__startswith="goc-").delete()

        async def get_or_create() -> None:
            for index in range(batch):
                await widgets.get_or_create(
                    name=f"goc-{index}", defaults={"category": CATEGORIES[index % 10], "value": index, "score": index}
                )

        results["get_or_create"] = await best(get_or_create, 3, clear_get_or_create)
        await clear_get_or_create()

        async def delete_bulk() -> None:
            await widgets.filter(category=CATEGORIES[2]).delete()

        results["delete_bulk"] = await best(delete_bulk, 1)
        await widgets.bulk_create(self.make_widgets("refill", rows // 10 + 1, category_offset=2))
        delete_ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def delete_loop() -> None:
            for primary_key in delete_ids:
                await (await widgets.get(id=primary_key)).delete()

        results["delete_loop"] = await best(delete_loop, 1)

    async def run_transaction_scenarios(self, rows: int, results: dict[str, float | None]) -> None:
        """The concurrent reads, the transactions and the JSON values."""
        orm = self.orm
        widgets = self.widgets
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        await widgets.bulk_create(self.make_widgets("widget", batch))
        concurrent_ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def concurrent_get() -> None:
            await asyncio.gather(*[widgets.get(id=primary_key) for primary_key in concurrent_ids])

        results["concurrent_get"] = await best(concurrent_get, 3)
        atomic_ids = [widget.id for widget in await widgets.all().limit(batch)]

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                async with orm.in_transaction():
                    widget = await widgets.get(id=primary_key)
                    widget.value = widget.value + 1
                    await widget.save(update_fields=["value"])

        results["atomic_update_loop"] = await best(atomic_update_loop, 3)

        async def nested_transaction_loop() -> None:
            async with orm.in_transaction():
                for primary_key in atomic_ids:
                    async with orm.in_transaction():
                        widget = await widgets.get(id=primary_key)
                        widget.value = widget.value + 1
                        await widget.save(update_fields=["value"])

        results["nested_transaction_loop"] = await best(nested_transaction_loop, 3)

        async def select_for_update_loop() -> None:
            for primary_key in atomic_ids:
                async with orm.in_transaction():
                    widget = await widgets.select_for_update().get(id=primary_key)
                    widget.value = widget.value + 1
                    await widget.save(update_fields=["value"])

        if "select_for_update_loop" not in self.excluded:
            results["select_for_update_loop"] = await best(select_for_update_loop, 3)

        async def json_write() -> None:
            await widgets.filter(category=CATEGORIES[3]).update(payload={"updated": True, "items": list(range(20))})

        results["json_write"] = await best(json_write, 5)

        async def json_read() -> None:
            for widget in await widgets.all():
                len(widget.payload)

        results["json_read"] = await best(json_read, 5)

    async def get_load_ids(self) -> list[int]:
        return [widget.id for widget in await self.widgets.all().limit(200)]

    async def run_load_test(self) -> dict[str, float]:
        widgets, ids = self.widgets, await self.get_load_ids()

        async def operation(index: int) -> None:
            operation_slot = index % 20
            if operation_slot < 16:
                await widgets.get(id=ids[index % len(ids)])
            elif operation_slot < 19:
                await widgets.filter(category=CATEGORIES[index % 10]).limit(10)
            else:
                widget = await widgets.get(id=ids[index % len(ids)])
                widget.value = widget.value + 1
                await widget.save(update_fields=["value"])

        return await LoadTest.run("load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)

    async def run_write_load_test(self) -> dict[str, float]:
        widgets, ids = self.widgets, await self.get_load_ids()

        async def operation(index: int) -> None:
            widget = await widgets.get(id=ids[index % len(ids)])
            if index % 2:
                widget.value = widget.value + 1
                await widget.save(update_fields=["value"])

        return await LoadTest.run("write_load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)
