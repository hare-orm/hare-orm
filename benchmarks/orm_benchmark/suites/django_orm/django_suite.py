from __future__ import annotations

import asyncio
from typing import Any

from orm_benchmark.constants import CATEGORIES, LOAD_CONCURRENCY, LOAD_OPERATIONS, POOL_MAX_SIZE
from orm_benchmark.declarations.databases import DATABASE_BY_KEY
from orm_benchmark.definitions.measurement import Measurement
from orm_benchmark.definitions.target import Target
from orm_benchmark.measuring.benchmark_scenario_run import BenchmarkScenarioRun
from orm_benchmark.measuring.load_test import LoadTest
from orm_benchmark.measuring.workload import Workload


class DjangoSuite:
    """The scenarios on Django's async ORM - on PostgreSQL with its psycopg connection pool, on SQLite
    with its own connection handling."""

    def __init__(self, target: Target, database: Any) -> None:
        """
        Args:
            target: The target.
            database: The run's ``PostgresqlDatabase`` or ``SqliteDatabase``.
        """
        import django
        from django.conf import settings

        self.target = target
        self.database = database
        self.excluded = DATABASE_BY_KEY[target.database].excluded_scenarios
        if target.database == "sqlite":
            database_settings = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(database.path)}
        else:
            database_settings = {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": database.name,
                "USER": "postgres",
                "PASSWORD": "postgres",
                "HOST": "127.0.0.1",
                "PORT": str(database.port),
                "OPTIONS": {"pool": {"min_size": 1, "max_size": POOL_MAX_SIZE}},
            }
        settings.configure(
            DEBUG=False,
            DATABASES={"default": database_settings},
            INSTALLED_APPS=["orm_benchmark.suites.django_orm"],
            USE_TZ=True,
            DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        )
        django.setup()
        from django.db import models

        class DjangoWidget(models.Model):
            name = models.CharField(max_length=100)
            category = models.CharField(max_length=20)
            value = models.IntegerField(default=0)
            score = models.FloatField(default=0.0)
            payload = models.JSONField(default=Workload.get_default_payload)
            tags = models.ManyToManyField("DjangoTag", related_name="widgets")

            class Meta:
                app_label = "django_orm"

        class DjangoGadget(models.Model):
            widget = models.ForeignKey(DjangoWidget, related_name="gadgets", on_delete=models.CASCADE)
            label = models.CharField(max_length=50)

            class Meta:
                app_label = "django_orm"

        class DjangoTag(models.Model):
            name = models.CharField(max_length=50)

            class Meta:
                app_label = "django_orm"

        self.widget, self.gadget, self.tag = DjangoWidget, DjangoGadget, DjangoTag

    def make_widgets(self, prefix: str, count: int, category_offset: int = 0) -> list[Any]:
        return [
            self.widget(
                name=f"{prefix}-{index}",
                category=CATEGORIES[(index + category_offset) % 10],
                value=index,
                score=index * 1.5,
            )
            for index in range(count)
        ]

    async def run(self, rows: int) -> dict[str, Measurement | None]:
        from asgiref.sync import sync_to_async
        from django.db import connection, connections

        def create_schema() -> None:
            with connection.schema_editor() as editor:
                editor.create_model(self.tag)
                editor.create_model(self.widget)
                editor.create_model(self.gadget)

        def first_query() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchall()

        async def connect() -> None:
            await sync_to_async(first_query)()

        async def touch() -> None:
            await self.widget.objects.all()[:1].aexists()

        try:
            cold_start = await BenchmarkScenarioRun.measure(connect, 1)
            await sync_to_async(create_schema)()
            async with asyncio.TaskGroup() as warmup:
                for _ in range(POOL_MAX_SIZE):
                    warmup.create_task(touch())
            results = await self.run_scenarios(rows)
            results["cold_start"] = cold_start
            await self.database.settle()
            results.update(await self.run_load_test())
            await self.database.settle()
            results.update(await self.run_write_load_test())
        finally:

            def close_connections() -> None:
                for database_connection in connections.all():
                    if self.target.database == "postgresql":
                        database_connection.close_pool()
                    database_connection.close()

            await sync_to_async(close_connections)()
        return results

    async def run_scenarios(self, rows: int) -> dict[str, Measurement | None]:
        results: dict[str, Measurement | None] = {}
        for run_group in (
            self.run_insert_scenarios,
            self.run_read_scenarios,
            self.run_filter_scenarios,
            self.run_relation_scenarios,
            self.run_aggregate_scenarios,
            self.run_update_scenarios,
            self.run_delete_scenarios,
            self.run_transaction_scenarios,
        ):
            await self.database.settle()
            await run_group(rows, results)
        return results

    async def run_insert_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The inserts - in bulk, of many rows and one by one."""
        widgets = self.widget.objects
        gadget_class = self.gadget
        tag_class = self.tag
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        async def clear_widgets() -> None:
            await widgets.all().adelete()

        async def bulk_create() -> None:
            await widgets.abulk_create(self.make_widgets("widget", rows))

        results["bulk_create"] = await measure(bulk_create, 3, clear_widgets)
        await clear_widgets()
        await widgets.abulk_create(self.make_widgets("widget", rows))
        all_widgets = [widget async for widget in widgets.all()]
        await gadget_class.objects.abulk_create(
            [gadget_class(widget=widget, label=f"gadget-of-{widget.name}") for widget in all_widgets]
        )
        tags = [await tag_class.objects.acreate(name=f"tag{index}") for index in range(5)]
        for index, widget in enumerate(all_widgets):
            await widget.tags.aadd(tags[index % len(tags)])

        async def clear_large() -> None:
            await widgets.filter(name__startswith="large-").adelete()

        async def bulk_create_large() -> None:
            await widgets.abulk_create(self.make_widgets("large", Workload.get_large_rows(rows)))

        results["bulk_create_large"] = await measure(bulk_create_large, 3, clear_large)
        await clear_large()

        async def single_insert() -> None:
            for index in range(batch):
                await widgets.acreate(
                    name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index
                )

        results["single_insert"] = await measure(single_insert, 3)

    async def run_read_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The reads of one model's rows - by key, the first, a page."""
        widgets = self.widget.objects
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        await widgets.filter(name__startswith="single-").adelete()

        async def fetch_all() -> None:
            [widget async for widget in widgets.all()]

        results["fetch_all"] = await measure(fetch_all, 5)
        ids = [widget.id async for widget in widgets.all()[:batch]]

        async def get_by_pk() -> None:
            for primary_key in ids:
                await widgets.aget(id=primary_key)

        results["get_by_pk"] = await measure(get_by_pk, 3)

        async def first_row() -> None:
            for index in range(batch):
                await widgets.filter(category=CATEGORIES[index % 10]).order_by("id").afirst()

        results["first_row"] = await measure(first_row, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            [widget async for widget in widgets.all().order_by("id")[page_size * 2 : page_size * 3]]

        results["paginated_fetch"] = await measure(paginated_fetch, 5)

        async def top_ten() -> None:
            [widget async for widget in widgets.all().order_by("-value")[:10]]

        results["top_ten"] = await measure(top_ten, 5)

    async def run_filter_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The filtered reads of one model's rows."""
        from django.db.models import Q

        widgets = self.widget.objects
        measure = BenchmarkScenarioRun.measure

        async def filter_rows() -> None:
            [widget async for widget in widgets.filter(category=CATEGORIES[0], value__gte=rows // 4)]

        results["filter"] = await measure(filter_rows, 5)

        async def icontains_search() -> None:
            [widget async for widget in widgets.filter(name__icontains="GET-1")]

        results["icontains_search"] = await measure(icontains_search, 5)

        async def exists_check() -> None:
            await widgets.filter(category=CATEGORIES[0]).aexists()

        results["exists_check"] = await measure(exists_check, 5)

        async def values_list_flat() -> None:
            [value async for value in widgets.filter(value__gte=rows // 4).values_list("id", flat=True)]

        results["values_list_flat"] = await measure(values_list_flat, 5)

        async def distinct_values() -> None:
            [value async for value in widgets.values_list("category", flat=True).distinct()]

        results["distinct_values"] = await measure(distinct_values, 5)

        async def only_fields() -> None:
            [widget async for widget in widgets.filter(value__gte=rows // 4).only("id", "name")]

        results["only_fields"] = await measure(only_fields, 5)

        async def complex_filter() -> None:
            [
                widget
                async for widget in widgets.filter(
                    Q(category=CATEGORIES[0]) | Q(category=CATEGORIES[1]),
                    value__gte=rows // 4,
                    gadgets__label__startswith="gadget-of-widget",
                )
                .order_by("value")
                .distinct()
            ]

        results["complex_filter"] = await measure(complex_filter, 5)
        large_in_ids = [widget.id async for widget in widgets.all()[: Workload.get_in_count(rows)]]

        async def large_in_filter() -> None:
            [widget async for widget in widgets.filter(id__in=large_in_ids).order_by("id")]

        results["large_in_filter"] = await measure(large_in_filter, 5)

    async def run_relation_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The reads across relations."""
        widgets = self.widget.objects
        gadget_class = self.gadget
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        async def iterate_chunks() -> None:
            async for widget in widgets.all().order_by("id").aiterator(chunk_size=200):
                widget.name  # noqa: B018 - read, as a caller would

        results["iterate_chunks"] = await measure(iterate_chunks, 5)
        prefetch_ids = [widget.id async for widget in widgets.all()[:batch]]

        async def prefetch_related() -> None:
            for widget in [widget async for widget in widgets.filter(id__in=prefetch_ids).prefetch_related("gadgets")]:
                [gadget async for gadget in widget.gadgets.all()]

        results["prefetch_related"] = await measure(prefetch_related, 5)

        async def prefetch_many_to_many() -> None:
            for widget in [widget async for widget in widgets.filter(id__in=prefetch_ids).prefetch_related("tags")]:
                [tag async for tag in widget.tags.all()]

        results["prefetch_many_to_many"] = await measure(prefetch_many_to_many, 5)

        async def n_plus_one() -> None:
            for widget in [widget async for widget in widgets.filter(id__in=prefetch_ids)]:
                [gadget async for gadget in widget.gadgets.all()]

        results["n_plus_one"] = await measure(n_plus_one, 3)
        gadget_ids = [gadget.id async for gadget in gadget_class.objects.all()[:batch]]

        async def select_related() -> None:
            for gadget in [
                gadget async for gadget in gadget_class.objects.filter(id__in=gadget_ids).select_related("widget")
            ]:
                gadget.widget.name  # noqa: B018 - already joined

        results["select_related_join"] = await measure(select_related, 5)

        async def related_filter() -> None:
            [gadget async for gadget in gadget_class.objects.filter(widget__category=CATEGORIES[0])]

        results["related_filter"] = await measure(related_filter, 5)

        async def many_to_many_filter() -> None:
            [widget async for widget in widgets.filter(tags__name="tag1")]

        results["many_to_many_filter"] = await measure(many_to_many_filter, 5)

    async def run_aggregate_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The annotations and the aggregates."""
        from django.db.models import Avg, Case, Count, Exists, F, Max, Min, OuterRef, Q, Sum, Value, When, Window
        from django.db.models.functions import Coalesce, Rank

        widgets = self.widget.objects
        gadget_class = self.gadget
        measure = BenchmarkScenarioRun.measure

        async def annotate_count() -> None:
            [row async for row in widgets.annotate(tag_count=Count("tags")).values("id", "tag_count")]

        results["annotate_count"] = await measure(annotate_count, 5)

        async def exists_annotation() -> None:
            has_gadget = Exists(gadget_class.objects.filter(widget=OuterRef("pk")))
            [row async for row in widgets.annotate(has_gadget=has_gadget).values("id", "has_gadget")]

        results["exists_annotation"] = await measure(exists_annotation, 5)

        async def count() -> None:
            await widgets.filter(value__gte=rows // 2).acount()

        results["count"] = await measure(count, 5)

        async def aggregate() -> None:
            await widgets.aaggregate(total=Sum("value"), avg_score=Avg("score"), hi=Max("value"), lo=Min("value"))

        results["aggregate"] = await measure(aggregate, 5)

        async def conditional_count() -> None:
            await widgets.aaggregate(high=Count("id", filter=Q(value__gte=rows // 2)))

        results["conditional_count"] = await measure(conditional_count, 5)

        async def group_by() -> None:
            [
                row
                async for row in widgets.values("category")
                .annotate(cnt=Count("id"), total=Sum("value"))
                .values("category", "cnt", "total")
            ]

        results["group_by"] = await measure(group_by, 5)
        having_threshold = rows * rows // 20

        async def having() -> None:
            [
                row
                async for row in widgets.values("category")
                .annotate(total=Sum("value"))
                .filter(total__gt=having_threshold)
                .values("category", "total")
            ]

        results["having"] = await measure(having, 5)

        async def annotate_values() -> None:
            [
                row
                async for row in widgets.annotate(doubled=Coalesce("value", 0)).values(
                    "id", "name", "value", "doubled"
                )
            ]

        results["annotate_values"] = await measure(annotate_values, 5)

        async def case_when() -> None:
            bucket = Case(When(value__gte=rows // 2, then=Value("high")), default=Value("low"))
            [row async for row in widgets.annotate(bucket=bucket).values("id", "bucket")]

        results["case_when_conditional"] = await measure(case_when, 5)

        async def window_rank() -> None:
            rank = Window(Rank(), partition_by=[F("category")], order_by=F("value").desc())
            [row async for row in widgets.annotate(rank=rank).values("id", "rank")]

        results["window_rank"] = await measure(window_rank, 5)

    async def run_update_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The updates and the upserts."""
        from django.db.models import F

        widgets = self.widget.objects
        widget_class = self.widget
        tag_class = self.tag
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        async def update_bulk() -> None:
            await widgets.filter(category=CATEGORIES[1]).aupdate(value=0)

        results["update_bulk"] = await measure(update_bulk, 3)

        async def update_expression() -> None:
            await widgets.filter(category=CATEGORIES[4]).aupdate(value=F("value") + 1)

        results["update_expression"] = await measure(update_expression, 3)
        loop_ids = [widget.id async for widget in widgets.all()[:batch]]

        async def update_by_pk() -> None:
            for primary_key in loop_ids:
                await widgets.filter(id=primary_key).aupdate(value=primary_key)

        results["update_by_pk"] = await measure(update_by_pk, 3)

        async def update_loop() -> None:
            for primary_key in loop_ids:
                widget = await widgets.aget(id=primary_key)
                widget.value = widget.value + 1
                await widget.asave(update_fields=["value"])

        results["update_loop"] = await measure(update_loop, 3)

        async def bulk_update() -> None:
            widgets_to_update = [widget async for widget in widgets.all()[:batch]]
            for widget in widgets_to_update:
                widget.value = widget.value + 100
            await widgets.abulk_update(widgets_to_update, fields=["value"])

        results["bulk_update"] = await measure(bulk_update, 3)
        many_to_many_widgets = [widget async for widget in widgets.all()[:batch]]
        extra_tag = await tag_class.objects.acreate(name="tag_extra")

        async def many_to_many_add() -> None:
            for widget in many_to_many_widgets:
                await widget.tags.aadd(extra_tag)

        results["many_to_many_add"] = await measure(many_to_many_add, 1)
        await extra_tag.widgets.aclear()
        upsert_widgets = [widget async for widget in widgets.all()[:batch]]

        async def upsert() -> None:
            await widgets.abulk_create(
                [
                    widget_class(
                        id=widget.id,
                        name=widget.name,
                        category=widget.category,
                        value=widget.value + 1,
                        score=widget.score,
                    )
                    for widget in upsert_widgets
                ],
                update_conflicts=True,
                unique_fields=["id"],
                update_fields=["value"],
            )

        results["upsert"] = await measure(upsert, 3)

    async def run_delete_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """get_or_create() and the deletes."""
        widgets = self.widget.objects
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        async def clear_get_or_create() -> None:
            await widgets.filter(name__startswith="goc-").adelete()

        async def get_or_create() -> None:
            for index in range(batch):
                await widgets.aget_or_create(
                    name=f"goc-{index}", defaults={"category": CATEGORIES[index % 10], "value": index, "score": index}
                )

        results["get_or_create"] = await measure(get_or_create, 3, clear_get_or_create)
        await clear_get_or_create()

        async def delete_bulk() -> None:
            await widgets.filter(category=CATEGORIES[2]).adelete()

        results["delete_bulk"] = await measure(delete_bulk, 1)
        await widgets.abulk_create(self.make_widgets("refill", rows // 10 + 1, category_offset=2))
        delete_ids = [widget.id async for widget in widgets.all()[:batch]]

        async def delete_loop() -> None:
            for primary_key in delete_ids:
                await (await widgets.aget(id=primary_key)).adelete()

        results["delete_loop"] = await measure(delete_loop, 1)

    async def run_transaction_scenarios(self, rows: int, results: dict[str, Measurement | None]) -> None:
        """The concurrent reads, the transactions and the JSON values."""
        from asgiref.sync import sync_to_async
        from django.db import transaction

        widgets = self.widget.objects
        widget_class = self.widget
        measure = BenchmarkScenarioRun.measure
        batch = Workload.get_batch(rows)

        await widgets.abulk_create(self.make_widgets("widget", batch))
        concurrent_ids = [widget.id async for widget in widgets.all()[:batch]]

        async def concurrent_get() -> None:
            await BenchmarkScenarioRun.concurrent(widgets.aget(id=primary_key) for primary_key in concurrent_ids)

        results["concurrent_get"] = await measure(concurrent_get, 3)
        atomic_ids = [widget.id async for widget in widgets.all()[:batch]]

        def update_one_in_transaction(primary_key: int) -> None:
            with transaction.atomic():
                widget = widget_class.objects.get(id=primary_key)
                widget.value = widget.value + 1
                widget.save(update_fields=["value"])

        update_in_transaction = sync_to_async(update_one_in_transaction)

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                await update_in_transaction(primary_key)

        results["atomic_update_loop"] = await measure(atomic_update_loop, 3)

        def update_each_in_nested_transaction() -> None:
            with transaction.atomic():
                for primary_key in atomic_ids:
                    with transaction.atomic():
                        widget = widget_class.objects.get(id=primary_key)
                        widget.value = widget.value + 1
                        widget.save(update_fields=["value"])

        nested_transaction_loop = sync_to_async(update_each_in_nested_transaction)
        results["nested_transaction_loop"] = await measure(nested_transaction_loop, 3)

        def update_one_locked(primary_key: int) -> None:
            with transaction.atomic():
                widget = widget_class.objects.select_for_update().get(id=primary_key)
                widget.value = widget.value + 1
                widget.save(update_fields=["value"])

        update_locked = sync_to_async(update_one_locked)

        async def select_for_update_loop() -> None:
            for primary_key in atomic_ids:
                await update_locked(primary_key)

        if "select_for_update_loop" not in self.excluded:
            results["select_for_update_loop"] = await measure(select_for_update_loop, 3)

        async def json_write() -> None:
            await widgets.filter(category=CATEGORIES[3]).aupdate(payload={"updated": True, "items": list(range(20))})

        results["json_write"] = await measure(json_write, 5)

        async def json_read() -> None:
            for widget in [widget async for widget in widgets.all()]:
                len(widget.payload)

        results["json_read"] = await measure(json_read, 5)

    async def get_load_ids(self) -> list[int]:
        return [widget.id async for widget in self.widget.objects.all()[:200]]

    async def run_load_test(self) -> dict[str, Measurement]:
        widgets, ids = self.widget.objects, await self.get_load_ids()

        async def operation(index: int) -> None:
            operation_slot = index % 20
            if operation_slot < 16:
                await widgets.aget(id=ids[index % len(ids)])
            elif operation_slot < 19:
                [widget async for widget in widgets.filter(category=CATEGORIES[index % 10])[:10]]
            else:
                widget = await widgets.aget(id=ids[index % len(ids)])
                widget.value = widget.value + 1
                await widget.asave(update_fields=["value"])

        return await LoadTest.run("load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)

    async def run_write_load_test(self) -> dict[str, Measurement]:
        widgets, ids = self.widget.objects, await self.get_load_ids()

        async def operation(index: int) -> None:
            widget = await widgets.aget(id=ids[index % len(ids)])
            if index % 2:
                widget.value = widget.value + 1
                await widget.asave(update_fields=["value"])

        return await LoadTest.run("write_load_test", operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)
