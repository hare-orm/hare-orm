"""hare-orm's comparative benchmark: the same scenarios - reads, relations, aggregates, writes,
transactions, a cold start and a concurrent load test - on hare-orm (its Rust and its asyncpg
PostgreSQL driver), SQLAlchemy (with a transaction around each session and in autocommit),
tortoise-orm, yara-orm and Django, against one real PostgreSQL server.

Commands:
    python benchmarks/bench.py all                   every ORM once, then the report (--runs N: N each)
    python benchmarks/bench.py run hare-rust         one run of one ORM, printed
    python benchmarks/bench.py charts                the charts, again, from results.json

``all`` runs every ORM in a process of its own, run after run in turn, writes the medians and every
run's numbers to docs/assets/benchmarks/results.json, draws the charts and rewrites every text about
the benchmark from them: the benchmark pages of the docs, the Benchmarks section of both READMEs and
the scenario and method lists of benchmarks/README.md. ``charts`` redoes all of that from
results.json without measuring. See benchmarks/README.md for the setup.
"""

import argparse
import asyncio
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

BENCHMARK_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY = BENCHMARK_DIRECTORY.parent
CHART_DIRECTORY = REPOSITORY / "docs" / "assets" / "benchmarks"
RESULTS_FILE = CHART_DIRECTORY / "results.json"

CATEGORIES = [f"cat{index}" for index in range(10)]
#: Every ORM gets the same pool - their defaults differ (5 to 16), and a smaller pool would starve
#: the load test's 50 workers instead of measuring the ORM.
POOL_MAX_SIZE = 50
SIZES = {"small": 100, "large": 1000}
LOAD_CONCURRENCY = 50
LOAD_OPERATIONS = 1000


class Workload:
    """How much work the scenarios do for a table of ``rows`` rows."""

    @staticmethod
    def get_batch(rows: int) -> int:
        """How many rows a per-row scenario (``get()`` one by one, ``bulk_update``, ...) touches."""
        return max(5, rows // 5)

    @staticmethod
    def get_in_count(rows: int) -> int:
        """How many values the ``id IN (...)`` scenario filters by."""
        return min(rows, 200)

    @classmethod
    def get_counts(cls, rows: int) -> dict[str, int]:
        """The counts the scenario labels name: ``rows``, ``batch`` and ``in_count``."""
        return {"rows": rows, "batch": cls.get_batch(rows), "in_count": cls.get_in_count(rows)}


@dataclass(frozen=True)
class Target:
    """One ORM set-up the benchmark measures.

    Attributes:
        key: The name on the command line and in results.json.
        name: The name on the charts.
        suite: Which scenario implementation runs it: ``active_record`` (hare, tortoise-orm,
            yara-orm - one shared API shape), ``django`` or ``sqlalchemy``.
        distribution: The package whose version the results record.
        light_colour: The series colour on a light background.
        dark_colour: The series colour on a dark background.
        driver_english: How the pages name the target's driver after "on" - for a target of
            ``BASELINE_DISTRIBUTION``, whose drivers the summary compares.
        driver_russian: The same after the Russian "на".
    """

    key: str
    name: str
    suite: str
    distribution: str
    light_colour: str
    dark_colour: str
    driver_english: str = ""
    driver_russian: str = ""


#: The chart order is the colour order: the colours are the dataviz reference palette's steps in an
#: order that passes its colour-blind and contrast checks on both backgrounds.
TARGETS = (
    Target(
        "hare-rust",
        "hare (Rust driver)",
        "active_record",
        "hare-orm",
        "#eb6834",
        "#d95926",
        "the Rust driver",
        "драйвере на Rust",
    ),
    Target(
        "hare-asyncpg",
        "hare (asyncpg)",
        "active_record",
        "hare-orm",
        "#2a78d6",
        "#3987e5",
        "asyncpg",
        "драйвере asyncpg",
    ),
    Target("sqlalchemy", "SQLAlchemy", "sqlalchemy", "SQLAlchemy", "#1baf7a", "#199e70"),
    Target("sqlalchemy-autocommit", "SQLAlchemy (autocommit)", "sqlalchemy", "SQLAlchemy", "#eda100", "#c98500"),
    Target("tortoise", "tortoise-orm", "active_record", "tortoise-orm", "#e87ba4", "#d55181"),
    Target("yara-orm", "yara-orm", "active_record", "yara-orm", "#4a3aa7", "#9085e9"),
    Target("django", "Django", "django", "Django", "#008300", "#008300"),
)
TARGET_BY_KEY = {target.key: target for target in TARGETS}
#: The summary compares every target with this package's fastest driver of the run (see
#: ``ChartWriter.get_baseline()``); its other drivers are compared with it too.
BASELINE_DISTRIBUTION = "hare-orm"

#: The scenarios by group, as the charts and the pages show them: key -> (English label, Russian
#: label). ``{rows}``, ``{batch}`` and ``{in_count}`` are the table size and the ``Workload`` counts.
SCENARIO_GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "reads",
        "Reads",
        "Чтение",
        (
            ("fetch_all", "All {rows} rows", "Все {rows} строк"),
            ("get_by_pk", "{batch} × get() by primary key", "{batch} × get() по ключу"),
            ("paginated_fetch", "A page, LIMIT/OFFSET", "Страница, LIMIT/OFFSET"),
            ("filter", "Filter on two fields", "Фильтр по двум полям"),
            ("exists_check", "exists()", "exists()"),
            ("values_list_flat", "values_list(flat=True)", "values_list(flat=True)"),
            ("only_fields", 'only("id", "name")', 'only("id", "name")'),
            ("complex_filter", "OR + JOIN + distinct", "OR + JOIN + distinct"),
            ("large_in_filter", "id IN ({in_count} values)", "id IN ({in_count} значений)"),
            ("json_read", "Reading a JSON field", "Чтение JSON-поля"),
        ),
    ),
    (
        "relations",
        "Relations",
        "Связи",
        (
            ("select_related_join", "select_related, a JOIN", "select_related, JOIN"),
            ("prefetch_related", "prefetch_related", "prefetch_related"),
            ("n_plus_one", "N+1: {batch} separate queries", "N+1: {batch} отдельных запросов"),
            ("m2m_add", "{batch} × many-to-many add()", "{batch} × add() многие-ко-многим"),
        ),
    ),
    (
        "aggregates",
        "Aggregates",
        "Агрегаты",
        (
            ("count", "count()", "count()"),
            ("aggregate", "Sum, Avg, Max, Min", "Sum, Avg, Max, Min"),
            ("group_by", "GROUP BY", "GROUP BY"),
            ("annotate_values", "annotate + values", "annotate + values"),
            ("case_when_conditional", "Case / When", "Case / When"),
        ),
    ),
    (
        "writes",
        "Writes",
        "Запись",
        (
            ("bulk_create", "bulk_create, {rows} rows", "bulk_create, {rows} строк"),
            ("single_insert", "{batch} × create()", "{batch} × create()"),
            ("update_bulk", "update() by a filter", "update() по фильтру"),
            ("update_loop", "{batch} × get() + save()", "{batch} × get() + save()"),
            ("bulk_update", "bulk_update, {batch} rows", "bulk_update, {batch} строк"),
            ("upsert", "Upsert, {batch} rows", "Upsert, {batch} строк"),
            ("get_or_create", "{batch} × get_or_create()", "{batch} × get_or_create()"),
            ("delete_bulk", "delete() by a filter", "delete() по фильтру"),
            ("delete_loop", "{batch} × get() + delete()", "{batch} × get() + delete()"),
            ("json_write", "Writing a JSON field", "Запись JSON-поля"),
        ),
    ),
    (
        "transactions",
        "Transactions, concurrency, start",
        "Транзакции, параллельность, запуск",
        (
            ("atomic_update_loop", "{batch} × a transaction: get() + save()", "{batch} × транзакция: get() + save()"),
            ("concurrent_get", "{batch} × get() at once", "{batch} × get() одновременно"),
            ("cold_start", "Start: init and the first connection", "Запуск: init и первое подключение"),
        ),
    ),
)


def default_payload() -> dict[str, Any]:
    """The JSON document every widget starts with."""
    return {
        "tags": ["red", "blue", "green"],
        "dimensions": {"w": 10, "h": 20, "d": 5},
        "history": [{"at": index, "note": f"event-{index}"} for index in range(10)],
        "active": True,
        "notes": "a medium-sized JSON document for the json_read/json_write scenarios",
    }


class Timing:
    """How a scenario is timed."""

    @staticmethod
    async def get_best(scenario: Callable[[], Awaitable[Any]], repetitions: int) -> float:
        """The fastest of ``repetitions`` runs of a scenario, in milliseconds.

        Args:
            scenario: The scenario.
            repetitions: How many times it runs; 1 for a scenario that changes the data for good.

        Returns:
            The fastest time.
        """
        times = []
        for __ in range(repetitions):
            start = time.perf_counter()
            await scenario()
            times.append((time.perf_counter() - start) * 1000)
        return min(times)


class LoadTest:
    """Sustained concurrent load: workers pull operations off a shared counter - 80% primary-key
    reads, 15% filtered reads, 5% single-row read-modify-write updates - until all are done, all
    racing for the same connection pool."""

    @staticmethod
    async def run(operation: Callable[[int], Awaitable[Any]], concurrency: int, total: int) -> dict[str, float]:
        """Runs the load test after an untimed warm-up round.

        Args:
            operation: Runs operation number ``index`` - its kind follows ``index % 20``.
            concurrency: How many workers run at once.
            total: How many operations the timed round runs.

        Returns:
            The wall-clock time, the throughput and the average latency under the load.
        """
        lock = asyncio.Lock()

        async def worker(counter: dict[str, int], operations: int) -> None:
            while True:
                async with lock:
                    if counter["done"] >= operations:
                        return
                    index = counter["done"]
                    counter["done"] += 1
                await operation(index)

        # Every pool connection is opened before the timed round - opening them is a one-time cost,
        # not the ORM's per-operation overhead.
        warm_up = {"done": 0}
        await asyncio.gather(*[worker(warm_up, concurrency) for __ in range(concurrency)])
        counter = {"done": 0}
        start = time.perf_counter()
        await asyncio.gather(*[worker(counter, total) for __ in range(concurrency)])
        elapsed_seconds = time.perf_counter() - start
        return {
            "load_test_total_ms": elapsed_seconds * 1000,
            "load_test_throughput_ops_per_sec": total / elapsed_seconds,
            "load_test_avg_latency_ms": elapsed_seconds * 1000 / total * concurrency,
        }


class ServerDatabase:
    """The throwaway database a run creates on the server and drops afterwards."""

    def __init__(self, port: int, name: str) -> None:
        """
        Args:
            port: The port of the PostgreSQL server at 127.0.0.1.
            name: The database's name.
        """
        self.port = port
        self.name = name

    async def connect(self) -> Any:
        """A connection to the server's ``postgres`` database."""
        import asyncpg

        return await asyncpg.connect(
            host="127.0.0.1", port=self.port, user="postgres", password="postgres", database="postgres"
        )

    async def create(self) -> str:
        """Creates the database afresh.

        Returns:
            The server's version.
        """
        connection = await self.connect()
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
            await connection.execute(f'CREATE DATABASE "{self.name}"')
            return str(await connection.fetchval("SHOW server_version"))
        finally:
            await connection.close()

    async def drop(self) -> None:
        """Drops the database - a failure here costs nothing but a leftover database, which the
        next run's create() drops."""
        try:
            connection = await self.connect()
            try:
                await connection.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
            finally:
                await connection.close()
        except Exception as error:  # noqa: BLE001 - never lose a finished run over the clean-up
            print(f"(the database {self.name} stays: {error})")


# --- hare-orm, tortoise-orm and yara-orm ------------------------------------------------------
# The three share one API shape. hare and tortoise find their models by scanning the module, so
# the models are module-level classes of the ORM the command line names.


class ActiveRecordOrm:
    """What one of hare, tortoise-orm and yara-orm provides to the scenarios."""

    def __init__(self, target_key: str) -> None:
        """
        Args:
            target_key: ``hare-rust``, ``hare-asyncpg``, ``tortoise`` or ``yara-orm``.
        """
        self.target_key = target_key
        if target_key.startswith("hare"):
            import hare
            import hare.fields
            import hare.query.functions as functions
            from hare.query.expressions import Case, Q, When
            from hare.transactions.transactions import Transactions

            self.model = hare.Model
            self.init_class = hare.Hare
            self.fields = hare.fields
            self.functions = functions
            self.case, self.when, self.q = Case, When, Q
            self.in_transaction = Transactions.atomic
        elif target_key == "tortoise":
            import tortoise
            import tortoise.fields
            import tortoise.functions as functions
            from tortoise.expressions import Case, Q, When
            from tortoise.transactions import in_transaction

            self.model = tortoise.Model
            self.init_class = tortoise.Tortoise
            self.fields = tortoise.fields
            self.functions = functions
            self.case, self.when, self.q = Case, When, Q
            self.in_transaction = in_transaction
        else:
            import yara_orm

            self.model = yara_orm.Model
            self.init_class = yara_orm.YaraOrm
            self.fields = yara_orm.fields
            self.functions = yara_orm
            self.coalesce = yara_orm.functions.Coalesce
            self.case, self.when, self.q = yara_orm.Case, yara_orm.When, yara_orm.Q
            self.in_transaction = yara_orm.in_transaction
        if not hasattr(self, "coalesce"):
            self.coalesce = self.functions.Coalesce

    def get_queries(self, model: Any) -> Any:
        """Where a model's queries start, as the ORM's documentation writes them: hare's manager
        (``Model.objects``), the model class itself on tortoise-orm and yara-orm.

        Args:
            model: The model.

        Returns:
            The manager or the class.
        """
        return model.objects if self.target_key.startswith("hare") else model

    def get_db_url(self, port: int, database: str) -> str:
        """The URL of the run's database, with the shared pool size.

        Args:
            port: The server's port.
            database: The database.

        Returns:
            The URL.
        """
        if self.target_key == "hare-rust":
            scheme, pool_parameter = "postgresql", "max_size"
        elif self.target_key == "hare-asyncpg":
            scheme, pool_parameter = "postgresql+asyncpg", "max_size"
        elif self.target_key == "tortoise":
            scheme, pool_parameter = "postgres", "maxsize"
        else:
            scheme, pool_parameter = "postgres", "max_size"
        return f"{scheme}://postgres:postgres@127.0.0.1:{port}/{database}?{pool_parameter}={POOL_MAX_SIZE}"


def get_requested_target() -> str | None:
    """The target of a ``run`` command line, read before the models are defined."""
    if len(sys.argv) > 2 and sys.argv[1] == "run":
        return sys.argv[2]
    return None


REQUESTED_TARGET = get_requested_target()
ACTIVE_RECORD_ORM = (
    ActiveRecordOrm(REQUESTED_TARGET)
    if REQUESTED_TARGET in TARGET_BY_KEY and TARGET_BY_KEY[REQUESTED_TARGET].suite == "active_record"
    else None
)

if ACTIVE_RECORD_ORM is not None:
    fields = ACTIVE_RECORD_ORM.fields

    class Widget(ACTIVE_RECORD_ORM.model):  # type: ignore[name-defined]
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100)
        category = fields.CharField(max_length=20)
        value = fields.IntField(default=0)
        score = fields.FloatField(default=0.0)
        payload = fields.JSONField(default=default_payload)
        tags = fields.ManyToManyField("models.Tag", related_name="widgets")

        class Meta:
            app = "models"

    class Gadget(ACTIVE_RECORD_ORM.model):  # type: ignore[name-defined]
        id = fields.IntField(primary_key=True)
        widget = fields.ForeignKeyField("models.Widget", related_name="gadgets")
        label = fields.CharField(max_length=50)

        class Meta:
            app = "models"

    class Tag(ACTIVE_RECORD_ORM.model):  # type: ignore[name-defined]
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)

        class Meta:
            app = "models"


class ActiveRecordSuite:
    """The scenarios on hare, tortoise-orm or yara-orm."""

    def __init__(self, orm: ActiveRecordOrm) -> None:
        self.orm = orm
        self.widgets = orm.get_queries(Widget)
        self.gadgets = orm.get_queries(Gadget)
        self.tags = orm.get_queries(Tag)

    async def seed(self, count: int) -> None:
        await self.widgets.bulk_create(
            [
                Widget(name=f"widget-{index}", category=CATEGORIES[index % 10], value=index, score=index * 1.5)
                for index in range(count)
            ]
        )

    async def seed_relations(self) -> None:
        """One gadget per widget, and five tags linked in turn."""
        widgets = await self.widgets.all()
        await self.gadgets.bulk_create([Gadget(widget=widget, label=f"gadget-of-{widget.name}") for widget in widgets])
        tags = [await self.tags.create(name=f"tag{index}") for index in range(5)]
        for index, widget in enumerate(widgets):
            await widget.tags.add(tags[index % len(tags)])

    async def run(self, port: int, database: ServerDatabase, rows: int) -> dict[str, float]:
        """Runs every scenario and the load test.

        Args:
            port: The server's port.
            database: The run's database.
            rows: The table size.

        Returns:
            Each scenario's time in milliseconds, and the load test's figures.
        """
        start = time.perf_counter()
        await self.orm.init_class.init(
            config={
                "connections": {"default": self.orm.get_db_url(port, database.name)},
                "apps": {"models": {"models": ["__main__"], "default_connection": "default"}},
            }
        )
        cold_start = (time.perf_counter() - start) * 1000
        await self.orm.init_class.generate_schemas()
        await asyncio.gather(*[self.widgets.all().limit(1) for __ in range(POOL_MAX_SIZE)])
        try:
            results = await self.run_scenarios(rows)
            results["cold_start"] = cold_start
            results.update(await self.run_load_test())
        finally:
            await self.orm.init_class.close_connections()
        return results

    async def run_scenarios(self, rows: int) -> dict[str, float]:
        orm = self.orm
        functions = orm.functions
        results: dict[str, float] = {}
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def bulk_create() -> None:
            await self.widgets.all().delete()
            await self.widgets.bulk_create(
                [
                    Widget(name=f"widget-{index}", category=CATEGORIES[index % 10], value=index, score=index * 1.5)
                    for index in range(rows)
                ]
            )

        results["bulk_create"] = await best(bulk_create, 3)
        await self.seed(rows)
        await self.seed_relations()

        async def single_insert() -> None:
            for index in range(batch):
                await self.widgets.create(
                    name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index
                )

        results["single_insert"] = await best(single_insert, 3)
        await self.widgets.filter(name__startswith="single-").delete()

        async def fetch_all() -> None:
            await self.widgets.all()

        results["fetch_all"] = await best(fetch_all, 5)
        ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def get_by_pk() -> None:
            for primary_key in ids:
                await self.widgets.get(id=primary_key)

        results["get_by_pk"] = await best(get_by_pk, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            await self.widgets.all().order_by("id").limit(page_size).offset(page_size * 2)

        results["paginated_fetch"] = await best(paginated_fetch, 5)

        async def filter_rows() -> None:
            await self.widgets.filter(category=CATEGORIES[0], value__gte=rows // 4)

        results["filter"] = await best(filter_rows, 5)

        async def exists_check() -> None:
            await self.widgets.filter(category=CATEGORIES[0]).exists()

        results["exists_check"] = await best(exists_check, 5)

        async def values_list_flat() -> None:
            await self.widgets.filter(value__gte=rows // 4).values_list("id", flat=True)

        results["values_list_flat"] = await best(values_list_flat, 5)

        async def only_fields() -> None:
            await self.widgets.filter(value__gte=rows // 4).only("id", "name")

        results["only_fields"] = await best(only_fields, 5)

        async def complex_filter() -> None:
            await (
                self.widgets.filter(
                    orm.q(category=CATEGORIES[0]) | orm.q(category=CATEGORIES[1]),
                    value__gte=rows // 4,
                    gadgets__label__startswith="gadget-of-widget",
                )
                .order_by("value")
                .distinct()
            )

        results["complex_filter"] = await best(complex_filter, 5)
        large_in_ids = [widget.id for widget in await self.widgets.all().limit(Workload.get_in_count(rows))]

        async def large_in_filter() -> None:
            await self.widgets.filter(id__in=large_in_ids).order_by("id")

        results["large_in_filter"] = await best(large_in_filter, 5)
        prefetch_ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def prefetch_related() -> None:
            for widget in await self.widgets.filter(id__in=prefetch_ids).prefetch_related("gadgets"):
                [gadget async for gadget in widget.gadgets]

        results["prefetch_related"] = await best(prefetch_related, 5)

        async def n_plus_one() -> None:
            for widget in await self.widgets.filter(id__in=prefetch_ids):
                await widget.gadgets.all()

        results["n_plus_one"] = await best(n_plus_one, 3)
        gadget_ids = [gadget.id for gadget in await self.gadgets.all().limit(batch)]

        async def select_related() -> None:
            for gadget in await self.gadgets.filter(id__in=gadget_ids).select_related("widget"):
                gadget.widget.name  # noqa: B018 - already joined

        results["select_related_join"] = await best(select_related, 5)

        async def count() -> None:
            await self.widgets.filter(value__gte=rows // 2).count()

        results["count"] = await best(count, 5)

        async def aggregate() -> None:
            aggregates = {
                "total": functions.Sum("value"),
                "avg_score": functions.Avg("score"),
                "hi": functions.Max("value"),
                "lo": functions.Min("value"),
            }
            if orm.target_key.startswith("hare"):
                # hare follows Django: the one-row total is QuerySet.aggregate().
                await self.widgets.all().aggregate(**aggregates)
            else:
                await self.widgets.annotate(**aggregates).values("total", "avg_score", "hi", "lo")

        results["aggregate"] = await best(aggregate, 5)

        async def group_by() -> None:
            await (
                self.widgets.annotate(cnt=functions.Count("id"), total=functions.Sum("value"))
                .group_by("category")
                .values("category", "cnt", "total")
            )

        results["group_by"] = await best(group_by, 5)

        async def annotate_values() -> None:
            await self.widgets.annotate(doubled=orm.coalesce("value", 0)).values("id", "name", "value", "doubled")

        results["annotate_values"] = await best(annotate_values, 5)

        async def case_when() -> None:
            await self.widgets.annotate(
                bucket=orm.case(orm.when(value__gte=rows // 2, then="high"), default="low")
            ).values("id", "bucket")

        results["case_when_conditional"] = await best(case_when, 5)

        async def update_bulk() -> None:
            await self.widgets.filter(category=CATEGORIES[1]).update(value=0)

        results["update_bulk"] = await best(update_bulk, 3)
        loop_ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def update_loop() -> None:
            for primary_key in loop_ids:
                widget = await self.widgets.get(id=primary_key)
                widget.value = widget.value + 1
                await widget.save(update_fields=["value"])

        results["update_loop"] = await best(update_loop, 3)

        async def bulk_update() -> None:
            widgets = await self.widgets.all().limit(batch)
            for widget in widgets:
                widget.value = widget.value + 100
            await self.widgets.bulk_update(widgets, fields=["value"])

        results["bulk_update"] = await best(bulk_update, 3)
        m2m_widgets = await self.widgets.all().limit(batch)
        extra_tag = await self.tags.create(name="tag_extra")

        async def m2m_add() -> None:
            for widget in m2m_widgets:
                await widget.tags.add(extra_tag)

        results["m2m_add"] = await best(m2m_add, 1)
        await extra_tag.widgets.clear()
        upsert_widgets = await self.widgets.all().limit(batch)

        async def upsert() -> None:
            await self.widgets.bulk_create(
                [
                    Widget(
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

        async def get_or_create() -> None:
            # Its own rows go first, so every repetition measures the create branch.
            await self.widgets.filter(name__startswith="goc-").delete()
            for index in range(batch):
                await self.widgets.get_or_create(
                    name=f"goc-{index}", defaults={"category": CATEGORIES[index % 10], "value": index, "score": index}
                )

        results["get_or_create"] = await best(get_or_create, 3)
        await self.widgets.filter(name__startswith="goc-").delete()

        async def delete_bulk() -> None:
            await self.widgets.filter(category=CATEGORIES[2]).delete()

        results["delete_bulk"] = await best(delete_bulk, 1)
        await self.widgets.bulk_create(
            [
                Widget(name=f"refill-{index}", category=CATEGORIES[2], value=index, score=index)
                for index in range(rows // 10 + 1)
            ]
        )
        delete_ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def delete_loop() -> None:
            for primary_key in delete_ids:
                await (await self.widgets.get(id=primary_key)).delete()

        results["delete_loop"] = await best(delete_loop, 1)
        await self.seed(batch)
        concurrent_ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def concurrent_get() -> None:
            await asyncio.gather(*[self.widgets.get(id=primary_key) for primary_key in concurrent_ids])

        results["concurrent_get"] = await best(concurrent_get, 3)
        atomic_ids = [widget.id for widget in await self.widgets.all().limit(batch)]

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                async with orm.in_transaction():
                    widget = await self.widgets.get(id=primary_key)
                    widget.value = widget.value + 1
                    await widget.save(update_fields=["value"])

        results["atomic_update_loop"] = await best(atomic_update_loop, 3)

        async def json_write() -> None:
            await self.widgets.filter(category=CATEGORIES[3]).update(
                payload={"updated": True, "items": list(range(20))}
            )

        results["json_write"] = await best(json_write, 5)

        async def json_read() -> None:
            for widget in await self.widgets.all():
                len(widget.payload)

        results["json_read"] = await best(json_read, 5)
        return results

    async def run_load_test(self) -> dict[str, float]:
        ids = [widget.id for widget in await self.widgets.all().limit(200)]

        async def operation(index: int) -> None:
            kind = index % 20
            if kind < 16:
                await self.widgets.get(id=ids[index % len(ids)])
            elif kind < 19:
                await self.widgets.filter(category=CATEGORIES[index % 10]).limit(10)
            else:
                widget = await self.widgets.get(id=ids[index % len(ids)])
                widget.value = widget.value + 1
                await widget.save(update_fields=["value"])

        return await LoadTest.run(operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)


# --- Django -----------------------------------------------------------------------------------


class DjangoSuite:
    """The scenarios on Django's async ORM, with its psycopg connection pool."""

    def __init__(self, port: int, database: str) -> None:
        import django
        from django.conf import settings

        settings.configure(
            DEBUG=False,
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": database,
                    "USER": "postgres",
                    "PASSWORD": "postgres",
                    "HOST": "127.0.0.1",
                    "PORT": str(port),
                    "OPTIONS": {"pool": {"min_size": 1, "max_size": POOL_MAX_SIZE}},
                }
            },
            INSTALLED_APPS=["__main__"],
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
            payload = models.JSONField(default=default_payload)
            tags = models.ManyToManyField("DjangoTag", related_name="widgets")

            class Meta:
                app_label = "__main__"

        class DjangoGadget(models.Model):
            widget = models.ForeignKey(DjangoWidget, related_name="gadgets", on_delete=models.CASCADE)
            label = models.CharField(max_length=50)

            class Meta:
                app_label = "__main__"

        class DjangoTag(models.Model):
            name = models.CharField(max_length=50)

            class Meta:
                app_label = "__main__"

        self.widget, self.gadget, self.tag = DjangoWidget, DjangoGadget, DjangoTag

    async def run(self, port: int, database: ServerDatabase, rows: int) -> dict[str, float]:
        from asgiref.sync import sync_to_async
        from django.db import connection, connections

        def create_schema() -> None:
            with connection.schema_editor() as editor:
                editor.create_model(self.tag)
                editor.create_model(self.widget)
                editor.create_model(self.gadget)

        # Django connects on the first query - the schema is that query, so the time covers the
        # first real connection, as the other ORMs' does.
        start = time.perf_counter()
        await sync_to_async(create_schema)()
        cold_start = (time.perf_counter() - start) * 1000
        await asyncio.gather(*[self.widget.objects.all()[:1].aexists() for __ in range(POOL_MAX_SIZE)])
        try:
            results = await self.run_scenarios(rows)
            results["cold_start"] = cold_start
            results.update(await self.run_load_test())
        finally:

            def close_pools() -> None:
                for database_connection in connections.all():
                    database_connection.close_pool()

            await sync_to_async(close_pools)()
        return results

    async def run_scenarios(self, rows: int) -> dict[str, float]:
        from asgiref.sync import sync_to_async
        from django.db import transaction
        from django.db.models import Avg, Case, Count, Max, Min, Q, Sum, Value, When
        from django.db.models.functions import Coalesce

        widgets_manager = self.widget.objects
        widget_class, gadget_class, tag_class = self.widget, self.gadget, self.tag
        results: dict[str, float] = {}
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        def make_rows(prefix: str, count: int, category_offset: int = 0) -> list[Any]:
            return [
                widget_class(
                    name=f"{prefix}-{index}",
                    category=CATEGORIES[(index + category_offset) % 10],
                    value=index,
                    score=index * 1.5,
                )
                for index in range(count)
            ]

        async def bulk_create() -> None:
            await widgets_manager.all().adelete()
            await widgets_manager.abulk_create(make_rows("widget", rows))

        results["bulk_create"] = await best(bulk_create, 3)
        await widgets_manager.abulk_create(make_rows("widget", rows))
        widgets = [widget async for widget in widgets_manager.all()]
        await gadget_class.objects.abulk_create(
            [gadget_class(widget=widget, label=f"gadget-of-{widget.name}") for widget in widgets]
        )
        tags = [await tag_class.objects.acreate(name=f"tag{index}") for index in range(5)]
        for index, widget in enumerate(widgets):
            await widget.tags.aadd(tags[index % len(tags)])

        async def single_insert() -> None:
            for index in range(batch):
                await widgets_manager.acreate(
                    name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index
                )

        results["single_insert"] = await best(single_insert, 3)
        await widgets_manager.filter(name__startswith="single-").adelete()

        async def fetch_all() -> None:
            [widget async for widget in widgets_manager.all()]

        results["fetch_all"] = await best(fetch_all, 5)
        ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        async def get_by_pk() -> None:
            for primary_key in ids:
                await widgets_manager.aget(id=primary_key)

        results["get_by_pk"] = await best(get_by_pk, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            [widget async for widget in widgets_manager.all().order_by("id")[page_size * 2 : page_size * 3]]

        results["paginated_fetch"] = await best(paginated_fetch, 5)

        async def filter_rows() -> None:
            [widget async for widget in widgets_manager.filter(category=CATEGORIES[0], value__gte=rows // 4)]

        results["filter"] = await best(filter_rows, 5)

        async def exists_check() -> None:
            await widgets_manager.filter(category=CATEGORIES[0]).aexists()

        results["exists_check"] = await best(exists_check, 5)

        async def values_list_flat() -> None:
            [value async for value in widgets_manager.filter(value__gte=rows // 4).values_list("id", flat=True)]

        results["values_list_flat"] = await best(values_list_flat, 5)

        async def only_fields() -> None:
            [widget async for widget in widgets_manager.filter(value__gte=rows // 4).only("id", "name")]

        results["only_fields"] = await best(only_fields, 5)

        async def complex_filter() -> None:
            [
                widget
                async for widget in widgets_manager.filter(
                    Q(category=CATEGORIES[0]) | Q(category=CATEGORIES[1]),
                    value__gte=rows // 4,
                    gadgets__label__startswith="gadget-of-widget",
                )
                .order_by("value")
                .distinct()
            ]

        results["complex_filter"] = await best(complex_filter, 5)
        large_in_ids = [widget.id async for widget in widgets_manager.all()[: Workload.get_in_count(rows)]]

        async def large_in_filter() -> None:
            [widget async for widget in widgets_manager.filter(id__in=large_in_ids).order_by("id")]

        results["large_in_filter"] = await best(large_in_filter, 5)
        prefetch_ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        async def prefetch_related() -> None:
            for widget in [w async for w in widgets_manager.filter(id__in=prefetch_ids).prefetch_related("gadgets")]:
                [gadget async for gadget in widget.gadgets.all()]

        results["prefetch_related"] = await best(prefetch_related, 5)

        async def n_plus_one() -> None:
            for widget in [w async for w in widgets_manager.filter(id__in=prefetch_ids)]:
                [gadget async for gadget in widget.gadgets.all()]

        results["n_plus_one"] = await best(n_plus_one, 3)
        gadget_ids = [gadget.id async for gadget in gadget_class.objects.all()[:batch]]

        async def select_related() -> None:
            for gadget in [g async for g in gadget_class.objects.filter(id__in=gadget_ids).select_related("widget")]:
                gadget.widget.name  # noqa: B018 - already joined

        results["select_related_join"] = await best(select_related, 5)

        async def count() -> None:
            await widgets_manager.filter(value__gte=rows // 2).acount()

        results["count"] = await best(count, 5)

        async def aggregate() -> None:
            await widgets_manager.aaggregate(
                total=Sum("value"), avg_score=Avg("score"), hi=Max("value"), lo=Min("value")
            )

        results["aggregate"] = await best(aggregate, 5)

        async def group_by() -> None:
            [
                row
                async for row in widgets_manager.values("category")
                .annotate(cnt=Count("id"), total=Sum("value"))
                .values("category", "cnt", "total")
            ]

        results["group_by"] = await best(group_by, 5)

        async def annotate_values() -> None:
            [
                row
                async for row in widgets_manager.annotate(doubled=Coalesce("value", 0)).values(
                    "id", "name", "value", "doubled"
                )
            ]

        results["annotate_values"] = await best(annotate_values, 5)

        async def case_when() -> None:
            bucket = Case(When(value__gte=rows // 2, then=Value("high")), default=Value("low"))
            [row async for row in widgets_manager.annotate(bucket=bucket).values("id", "bucket")]

        results["case_when_conditional"] = await best(case_when, 5)

        async def update_bulk() -> None:
            await widgets_manager.filter(category=CATEGORIES[1]).aupdate(value=0)

        results["update_bulk"] = await best(update_bulk, 3)
        loop_ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        async def update_loop() -> None:
            for primary_key in loop_ids:
                widget = await widgets_manager.aget(id=primary_key)
                widget.value = widget.value + 1
                await widget.asave(update_fields=["value"])

        results["update_loop"] = await best(update_loop, 3)

        async def bulk_update() -> None:
            widgets_to_update = [widget async for widget in widgets_manager.all()[:batch]]
            for widget in widgets_to_update:
                widget.value = widget.value + 100
            await widgets_manager.abulk_update(widgets_to_update, fields=["value"])

        results["bulk_update"] = await best(bulk_update, 3)
        m2m_widgets = [widget async for widget in widgets_manager.all()[:batch]]
        extra_tag = await tag_class.objects.acreate(name="tag_extra")

        async def m2m_add() -> None:
            for widget in m2m_widgets:
                await widget.tags.aadd(extra_tag)

        results["m2m_add"] = await best(m2m_add, 1)
        await extra_tag.widgets.aclear()
        upsert_widgets = [widget async for widget in widgets_manager.all()[:batch]]

        async def upsert() -> None:
            await widgets_manager.abulk_create(
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

        results["upsert"] = await best(upsert, 3)

        async def get_or_create() -> None:
            await widgets_manager.filter(name__startswith="goc-").adelete()
            for index in range(batch):
                await widgets_manager.aget_or_create(
                    name=f"goc-{index}", defaults={"category": CATEGORIES[index % 10], "value": index, "score": index}
                )

        results["get_or_create"] = await best(get_or_create, 3)
        await widgets_manager.filter(name__startswith="goc-").adelete()

        async def delete_bulk() -> None:
            await widgets_manager.filter(category=CATEGORIES[2]).adelete()

        results["delete_bulk"] = await best(delete_bulk, 1)
        await widgets_manager.abulk_create(make_rows("refill", rows // 10 + 1, category_offset=2))
        delete_ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        async def delete_loop() -> None:
            for primary_key in delete_ids:
                await (await widgets_manager.aget(id=primary_key)).adelete()

        results["delete_loop"] = await best(delete_loop, 1)
        await widgets_manager.abulk_create(make_rows("widget", batch))
        concurrent_ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        async def concurrent_get() -> None:
            await asyncio.gather(*[widgets_manager.aget(id=primary_key) for primary_key in concurrent_ids])

        results["concurrent_get"] = await best(concurrent_get, 3)
        atomic_ids = [widget.id async for widget in widgets_manager.all()[:batch]]

        # transaction.atomic() has no async form: the documented way is a sync function run through
        # sync_to_async().
        def update_one_in_transaction(primary_key: int) -> None:
            with transaction.atomic():
                widget = widget_class.objects.get(id=primary_key)
                widget.value = widget.value + 1
                widget.save(update_fields=["value"])

        update_in_transaction = sync_to_async(update_one_in_transaction)

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                await update_in_transaction(primary_key)

        results["atomic_update_loop"] = await best(atomic_update_loop, 3)

        async def json_write() -> None:
            await widgets_manager.filter(category=CATEGORIES[3]).aupdate(
                payload={"updated": True, "items": list(range(20))}
            )

        results["json_write"] = await best(json_write, 5)

        async def json_read() -> None:
            for widget in [w async for w in widgets_manager.all()]:
                len(widget.payload)

        results["json_read"] = await best(json_read, 5)
        return results

    async def run_load_test(self) -> dict[str, float]:
        widgets_manager = self.widget.objects
        ids = [widget.id async for widget in widgets_manager.all()[:200]]

        async def operation(index: int) -> None:
            kind = index % 20
            if kind < 16:
                await widgets_manager.aget(id=ids[index % len(ids)])
            elif kind < 19:
                [widget async for widget in widgets_manager.filter(category=CATEGORIES[index % 10])[:10]]
            else:
                widget = await widgets_manager.aget(id=ids[index % len(ids)])
                widget.value = widget.value + 1
                await widget.asave(update_fields=["value"])

        return await LoadTest.run(operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)


# --- SQLAlchemy -------------------------------------------------------------------------------


class SqlAlchemySuite:
    """The scenarios on SQLAlchemy's 2.0 async ORM with asyncpg, written the way its documentation
    writes them: a fresh session per timed repetition, ``select()`` with loader options, ORM bulk
    INSERT/UPDATE, PostgreSQL's ``on_conflict_do_update()``.

    By default a session runs its statements in a transaction it commits (SQLAlchemy's own default).
    With ``autocommit`` the engine commits every statement on its own - as hare and Django do - and
    the one scenario that needs a transaction opens a real one on a second engine.
    """

    def __init__(self, autocommit: bool) -> None:
        self.autocommit = autocommit
        from sqlalchemy import Column, Float, ForeignKey, Integer, String, Table
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import AsyncAttrs
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
            payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=default_payload)
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
                "payload": default_payload(),
            }
            for index in range(count)
        ]

    async def run(self, port: int, database: ServerDatabase, rows: int) -> dict[str, float]:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.orm import configure_mappers

        start = time.perf_counter()
        engine = create_async_engine(
            f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{port}/{database.name}",
            pool_size=POOL_MAX_SIZE,
            max_overflow=0,
            **({"isolation_level": "AUTOCOMMIT"} if self.autocommit else {}),
        )
        configure_mappers()
        async with engine.begin() as connection:
            await connection.run_sync(self.base.metadata.create_all)
        cold_start = (time.perf_counter() - start) * 1000
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        # In autocommit, Session.begin() sends no BEGIN - the transaction scenario takes its
        # sessions from the same pool with the server's default isolation instead.
        transactional_factory = (
            async_sessionmaker(engine.execution_options(isolation_level="READ COMMITTED"), expire_on_commit=False)
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
        finally:
            await engine.dispose()
        return results

    async def run_scenarios(self, session_factory: Any, transactional_factory: Any, rows: int) -> dict[str, float]:
        from sqlalchemy import case, delete, exists, func, insert, or_, select, update
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert
        from sqlalchemy.orm import joinedload, load_only, selectinload

        widget_class, gadget_class, tag_class = self.widget, self.gadget, self.tag
        results: dict[str, float] = {}
        best = Timing.get_best
        batch = Workload.get_batch(rows)

        async def get_ids(model: Any, limit: int) -> list[int]:
            async with session_factory() as session:
                return list((await session.scalars(select(model.id).limit(limit))).all())

        async def bulk_create() -> None:
            async with session_factory() as session:
                await session.execute(delete(widget_class))
                await session.execute(insert(widget_class), self.make_rows("widget", rows))
                await session.commit()

        results["bulk_create"] = await best(bulk_create, 3)
        async with session_factory() as session:
            await session.execute(insert(widget_class), self.make_rows("widget", rows))
            await session.commit()
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

        async def single_insert() -> None:
            async with session_factory() as session:
                for index in range(batch):
                    session.add(
                        widget_class(name=f"single-{index}", category=CATEGORIES[index % 10], value=index, score=index)
                    )
                    await session.commit()

        results["single_insert"] = await best(single_insert, 3)
        async with session_factory() as session:
            await session.execute(delete(widget_class).where(widget_class.name.startswith("single-")))
            await session.commit()

        async def fetch_all() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class))).all()

        results["fetch_all"] = await best(fetch_all, 5)
        ids = await get_ids(widget_class, batch)

        async def get_by_pk() -> None:
            async with session_factory() as session:
                for primary_key in ids:
                    await session.get(widget_class, primary_key)

        results["get_by_pk"] = await best(get_by_pk, 3)
        page_size = max(5, rows // 10)

        async def paginated_fetch() -> None:
            async with session_factory() as session:
                statement = select(widget_class).order_by(widget_class.id).offset(page_size * 2).limit(page_size)
                (await session.scalars(statement)).all()

        results["paginated_fetch"] = await best(paginated_fetch, 5)

        async def filter_rows() -> None:
            async with session_factory() as session:
                statement = select(widget_class).where(
                    widget_class.category == CATEGORIES[0], widget_class.value >= rows // 4
                )
                (await session.scalars(statement)).all()

        results["filter"] = await best(filter_rows, 5)

        async def exists_check() -> None:
            async with session_factory() as session:
                await session.scalar(select(exists().where(widget_class.category == CATEGORIES[0])))

        results["exists_check"] = await best(exists_check, 5)

        async def values_list_flat() -> None:
            async with session_factory() as session:
                (await session.scalars(select(widget_class.id).where(widget_class.value >= rows // 4))).all()

        results["values_list_flat"] = await best(values_list_flat, 5)

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
        large_in_ids = await get_ids(widget_class, Workload.get_in_count(rows))

        async def large_in_filter() -> None:
            async with session_factory() as session:
                statement = select(widget_class).where(widget_class.id.in_(large_in_ids)).order_by(widget_class.id)
                (await session.scalars(statement)).all()

        results["large_in_filter"] = await best(large_in_filter, 5)
        prefetch_ids = await get_ids(widget_class, batch)

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

        async def n_plus_one() -> None:
            async with session_factory() as session:
                statement = select(widget_class).where(widget_class.id.in_(prefetch_ids))
                for widget in (await session.scalars(statement)).all():
                    await widget.awaitable_attrs.gadgets

        results["n_plus_one"] = await best(n_plus_one, 3)
        gadget_ids = await get_ids(gadget_class, batch)

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

        async def group_by() -> None:
            async with session_factory() as session:
                statement = select(
                    widget_class.category, func.count(widget_class.id), func.sum(widget_class.value)
                ).group_by(widget_class.category)
                (await session.execute(statement)).all()

        results["group_by"] = await best(group_by, 5)

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

        async def update_bulk() -> None:
            async with session_factory() as session:
                await session.execute(
                    update(widget_class).where(widget_class.category == CATEGORIES[1]).values(value=0)
                )
                await session.commit()

        results["update_bulk"] = await best(update_bulk, 3)
        loop_ids = await get_ids(widget_class, batch)

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
        # The widgets and the tag load before the timer and stay in one session, so appending to a
        # relation can flush.
        async with session_factory() as m2m_session:
            m2m_widgets = (await m2m_session.scalars(select(widget_class).limit(batch))).all()
            extra_tag = tag_class(name="tag_extra")
            m2m_session.add(extra_tag)
            await m2m_session.commit()

            async def m2m_add() -> None:
                for widget in m2m_widgets:
                    (await widget.awaitable_attrs.tags).append(extra_tag)
                    await m2m_session.commit()

            results["m2m_add"] = await best(m2m_add, 1)
            await m2m_session.execute(delete(self.widget_tags).where(self.widget_tags.c.tag_id == extra_tag.id))
            await m2m_session.commit()
        async with session_factory() as session:
            upsert_widgets = (await session.scalars(select(widget_class).limit(batch))).all()

        async def upsert() -> None:
            async with session_factory() as session:
                statement = postgresql_insert(widget_class).values(
                    [
                        {"id": w.id, "name": w.name, "category": w.category, "value": w.value + 1, "score": w.score}
                        for w in upsert_widgets
                    ]
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[widget_class.id], set_={"value": statement.excluded.value}
                )
                await session.execute(statement)
                await session.commit()

        results["upsert"] = await best(upsert, 3)

        async def get_or_create() -> None:
            async with session_factory() as session:
                await session.execute(delete(widget_class).where(widget_class.name.startswith("goc-")))
                await session.commit()
                for index in range(batch):
                    existing = await session.scalar(select(widget_class).where(widget_class.name == f"goc-{index}"))
                    if existing is None:
                        session.add(
                            widget_class(
                                name=f"goc-{index}", category=CATEGORIES[index % 10], value=index, score=index
                            )
                        )
                        await session.commit()

        results["get_or_create"] = await best(get_or_create, 3)
        async with session_factory() as session:
            await session.execute(delete(widget_class).where(widget_class.name.startswith("goc-")))
            await session.commit()

        async def delete_bulk() -> None:
            async with session_factory() as session:
                await session.execute(delete(widget_class).where(widget_class.category == CATEGORIES[2]))
                await session.commit()

        results["delete_bulk"] = await best(delete_bulk, 1)
        async with session_factory() as session:
            await session.execute(insert(widget_class), self.make_rows("refill", rows // 10 + 1, category_offset=2))
            await session.commit()
        delete_ids = await get_ids(widget_class, batch)

        async def delete_loop() -> None:
            async with session_factory() as session:
                for primary_key in delete_ids:
                    await session.delete(await session.get(widget_class, primary_key))
                    await session.commit()

        results["delete_loop"] = await best(delete_loop, 1)
        async with session_factory() as session:
            await session.execute(insert(widget_class), self.make_rows("widget", batch))
            await session.commit()
        concurrent_ids = await get_ids(widget_class, batch)

        async def get_in_own_session(primary_key: int) -> None:
            async with session_factory() as session:
                await session.get(widget_class, primary_key)

        async def concurrent_get() -> None:
            await asyncio.gather(*[get_in_own_session(primary_key) for primary_key in concurrent_ids])

        results["concurrent_get"] = await best(concurrent_get, 3)
        atomic_ids = await get_ids(widget_class, batch)

        async def atomic_update_loop() -> None:
            for primary_key in atomic_ids:
                async with transactional_factory.begin() as session:
                    widget = await session.get(widget_class, primary_key)
                    widget.value = widget.value + 1

        results["atomic_update_loop"] = await best(atomic_update_loop, 3)

        async def json_write() -> None:
            async with session_factory() as session:
                await session.execute(
                    update(widget_class)
                    .where(widget_class.category == CATEGORIES[3])
                    .values(payload={"updated": True, "items": list(range(20))})
                )
                await session.commit()

        results["json_write"] = await best(json_write, 5)

        async def json_read() -> None:
            async with session_factory() as session:
                for widget in (await session.scalars(select(widget_class))).all():
                    len(widget.payload)

        results["json_read"] = await best(json_read, 5)
        return results

    async def run_load_test(self, session_factory: Any) -> dict[str, float]:
        from sqlalchemy import select

        widget_class = self.widget
        async with session_factory() as session:
            ids = (await session.scalars(select(widget_class.id).limit(200))).all()

        async def operation(index: int) -> None:
            kind = index % 20
            async with session_factory() as session:
                if kind < 16:
                    await session.get(widget_class, ids[index % len(ids)])
                elif kind < 19:
                    statement = select(widget_class).where(widget_class.category == CATEGORIES[index % 10]).limit(10)
                    (await session.scalars(statement)).all()
                else:
                    widget = await session.get(widget_class, ids[index % len(ids)])
                    widget.value = widget.value + 1
                    await session.commit()

        return await LoadTest.run(operation, LOAD_CONCURRENCY, LOAD_OPERATIONS)


# --- Running --------------------------------------------------------------------------------


class Runner:
    """Runs the benchmark: one run of one ORM, or every ORM several times."""

    @classmethod
    def get_environment(cls) -> dict[str, Any]:
        """The machine and Python a run measures on."""
        return {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "processor": cls.get_processor_name(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        }

    @staticmethod
    def get_processor_name() -> str:
        """The processor's marketing name ("Intel Core i7-..."), where the system tells it."""
        try:
            if sys.platform == "win32":
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
                ) as key:
                    return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            if sys.platform == "darwin":
                return subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
                ).stdout.strip()
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError, subprocess.CalledProcessError:
            pass
        return platform.processor() or platform.machine()

    @staticmethod
    def get_version(distribution: str) -> str:
        """The installed version of a package, "not installed" without it."""
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            return "not installed"

    @classmethod
    async def run_once(cls, target: Target, size: str, port: int) -> dict[str, Any]:
        """One run of one ORM.

        Args:
            target: The ORM set-up.
            size: ``small`` or ``large``.
            port: The PostgreSQL server's port at 127.0.0.1.

        Returns:
            The run's results, versions and environment.
        """
        database = ServerDatabase(port, f"hare_bench_{target.key.replace('-', '_')}")
        server_version = await database.create()
        rows = SIZES[size]
        try:
            suite: ActiveRecordSuite | DjangoSuite | SqlAlchemySuite
            if target.suite == "active_record":
                assert ACTIVE_RECORD_ORM is not None
                suite = ActiveRecordSuite(ACTIVE_RECORD_ORM)
            elif target.suite == "django":
                suite = DjangoSuite(port, database.name)
            else:
                suite = SqlAlchemySuite(autocommit=target.key == "sqlalchemy-autocommit")
            results = await suite.run(port, database, rows)
        finally:
            await database.drop()
        return {
            "target": target.key,
            "size": size,
            "rows": rows,
            "version": cls.get_version(target.distribution),
            "asyncpg": cls.get_version("asyncpg"),
            "postgresql": server_version,
            "environment": cls.get_environment(),
            "results": results,
        }

    @classmethod
    def run_all(cls, targets: list[Target], runs: int, size: str, port: int) -> dict[str, Any]:
        """Every ORM ``runs`` times, each run in a process of its own - run 1 of every ORM, then
        run 2 of every ORM, and so on, so a slow moment of the machine doesn't land on one ORM.

        Args:
            targets: The ORM set-ups.
            runs: How many runs each gets.
            size: ``small`` or ``large``.
            port: The PostgreSQL server's port.

        Returns:
            The report: the medians and every run's numbers, with the versions and the machine.
        """
        by_target: dict[str, list[dict[str, Any]]] = {target.key: [] for target in targets}
        with tempfile.TemporaryDirectory() as run_directory:
            for run_number in range(1, runs + 1):
                round_order = list(targets)
                random.Random(run_number).shuffle(round_order)
                for target in round_order:
                    output = Path(run_directory) / f"{target.key}-{run_number}.json"
                    print(f"run {run_number}/{runs}: {target.key}", flush=True)
                    subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "run",
                            target.key,
                            "--size",
                            size,
                            "--port",
                            str(port),
                            "--out",
                            str(output),
                        ],
                        check=True,
                    )
                    by_target[target.key].append(json.loads(output.read_text(encoding="utf-8")))
        first_run = next(iter(by_target.values()))[0]
        report: dict[str, Any] = {
            "generated": datetime.now(UTC).strftime("%Y-%m-%d"),
            "size": size,
            "rows": SIZES[size],
            "runs": runs,
            "postgresql": first_run["postgresql"],
            "asyncpg": first_run["asyncpg"],
            "environment": first_run["environment"],
            "hare_commit": cls.get_hare_commit(),
            "targets": {},
        }
        for target in targets:
            target_runs = by_target[target.key]
            scenarios = {}
            for scenario in target_runs[0]["results"]:
                values = [run["results"][scenario] for run in target_runs]
                scenarios[scenario] = {"median": statistics.median(values), "runs": values}
            report["targets"][target.key] = {
                "name": target.name,
                "version": target_runs[0]["version"],
                "scenarios": scenarios,
            }
        return report

    @staticmethod
    def get_hare_commit() -> str | None:
        """The commit of the hare-orm checkout measured, None outside a git checkout."""
        try:
            return subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], cwd=REPOSITORY, capture_output=True, text=True, check=True
            ).stdout.strip()
        except OSError, subprocess.CalledProcessError:
            return None


# --- Charts ---------------------------------------------------------------------------------


class ChartWriter:
    """Draws the charts the documentation and the README show, as SVG files - one per language and
    colour scheme: the summary (how much slower than hare-orm each ORM is on average, and the
    load test's throughput) and one chart per scenario group, a group of bars per scenario."""

    FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    MONOSPACE = "ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace"
    #: ``surface`` is the chart's own background - the docs' page colour - so its text stays readable
    #: on any page, whichever of the two variants that page ends up showing.
    THEMES = {
        "light": {"ink": "#1e1b15", "secondary": "#57503f", "muted": "#6e6654", "surface": "#faf9f4"},
        "dark": {"ink": "#ede7d8", "secondary": "#c4bba5", "muted": "#a89e86", "surface": "#15130f"},
    }
    WIDTH = 760
    PADDING = 20

    def __init__(self, report: dict[str, Any], language: str, theme: str) -> None:
        """
        Args:
            report: The report of ``Runner.run_all()``.
            language: ``en`` or ``ru``.
            theme: ``light`` or ``dark``.
        """
        self.report = report
        self.language = language
        self.theme = theme
        self.colours = self.THEMES[theme]
        self.targets = [target for target in TARGETS if target.key in report["targets"]]
        self.counts = Workload.get_counts(report["rows"])

    def get_label(self, english: str, russian: str) -> str:
        """A scenario or group label in the chart's language, its counts filled in."""
        return (russian if self.language == "ru" else english).format(**self.counts)

    def get_series_colour(self, target: Target) -> str:
        return target.light_colour if self.theme == "light" else target.dark_colour

    def get_median(self, target: Target, scenario: str) -> float:
        return float(self.report["targets"][target.key]["scenarios"][scenario]["median"])

    def format_number(self, value: float, decimals: int) -> str:
        """A number with the language's decimal and thousands separators."""
        text = f"{value:,.{decimals}f}"
        if self.language == "ru":
            return text.replace(",", " ").replace(".", ",")
        return text

    def format_milliseconds(self, value: float) -> str:
        unit = "мс" if self.language == "ru" else "ms"
        decimals = 0 if value >= 100 else 1 if value >= 10 else 2
        return f"{self.format_number(value, decimals)} {unit}"

    @staticmethod
    def escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def text(self, x: float, y: float, content: str, **attributes: Any) -> str:
        """An SVG text element in the chart's font."""
        size = attributes.pop("size", 13)
        colour = attributes.pop("colour", self.colours["ink"])
        family = attributes.pop("family", self.FONT)
        extra = "".join(f' {name.replace("_", "-")}="{value}"' for name, value in attributes.items())
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" fill="{colour}"{extra}>'
            f"{self.escape(content)}</text>"
        )

    @staticmethod
    def bar(x: float, y: float, length: float, thickness: float, colour: str) -> str:
        """A horizontal bar, square at the baseline, its end rounded."""
        radius = min(4.0, length / 2, thickness / 2)
        return (
            f'<path d="M{x:.1f},{y:.1f} H{x + length - radius:.1f} Q{x + length:.1f},{y:.1f} {x + length:.1f},'
            f"{y + radius:.1f} V{y + thickness - radius:.1f} Q{x + length:.1f},{y + thickness:.1f} "
            f'{x + length - radius:.1f},{y + thickness:.1f} H{x:.1f} Z" fill="{colour}"/>'
        )

    def legend(self, y: float) -> tuple[list[str], float]:
        """The legend's items in rows across the chart's width.

        Args:
            y: The top of the legend.

        Returns:
            The SVG elements and the legend's height.
        """
        elements = []
        x, row = 0.0, 0
        for target in self.targets:
            item_width = 22 + len(target.name) * 7.2 + 18
            if x + item_width > self.WIDTH and x > 0:
                x, row = 0.0, row + 1
            top = y + row * 22
            elements.append(
                f'<rect x="{x:.1f}" y="{top + 3:.1f}" width="12" height="12" rx="3" '
                f'fill="{self.get_series_colour(target)}"/>'
            )
            elements.append(self.text(x + 18, top + 13.5, target.name, size=13))
            x += item_width
        return elements, (row + 1) * 22

    def document(self, height: float, elements: list[str], title: str) -> str:
        """The SVG file: the chart on its own rounded background, ``PADDING`` in from its edges."""
        width, full_height = self.WIDTH + 2 * self.PADDING, height + 2 * self.PADDING
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {full_height:.0f}" '
            f'width="{width}" height="{full_height:.0f}" role="img">'
            f"<title>{self.escape(title)}</title>"
            f'<rect width="{width}" height="{full_height:.0f}" rx="10" fill="{self.colours["surface"]}"/>'
            f'<g transform="translate({self.PADDING},{self.PADDING})">{"".join(elements)}</g></svg>\n'
        )

    def ranked_bars(self, title: str, rows: list[tuple[Target, float, str]]) -> str:
        """Bars in rank order, the name on the left and the value at the end - a summary chart.

        Args:
            title: The chart's title, for screen readers.
            rows: The ORM, its value and the value's text, best first.

        Returns:
            The SVG.
        """
        label_width, row_height = 200.0, 30.0
        maximum = max(value for __, value, __ in rows)
        plot_width = self.WIDTH - label_width - 90
        elements = []
        for index, (target, value, value_text) in enumerate(rows):
            y = index * row_height
            elements.append(self.text(label_width - 12, y + 19, target.name, size=13.5, text_anchor="end"))
            length = max(2.0, value / maximum * plot_width)
            elements.append(self.bar(label_width, y + 7, length, 16, self.get_series_colour(target)))
            elements.append(
                self.text(
                    label_width + length + 8,
                    y + 19.5,
                    value_text,
                    size=12.5,
                    colour=self.colours["secondary"],
                    family=self.MONOSPACE,
                )
            )
        return self.document(len(rows) * row_height + 6, elements, title)

    def get_latency_scenarios(self) -> list[str]:
        return [key for __, __, __, scenarios in SCENARIO_GROUPS for key, __, __ in scenarios if key != "cold_start"]

    def get_geometric_mean_time(self, target: Target) -> float:
        """The geometric mean of a target's medians over every scenario but the cold start."""
        scenarios = self.get_latency_scenarios()
        logarithms = [math.log(self.get_median(target, scenario)) for scenario in scenarios]
        return math.exp(sum(logarithms) / len(logarithms))

    def get_baseline_drivers(self) -> list[Target]:
        """The targets of ``BASELINE_DISTRIBUTION`` in the report, fastest first."""
        drivers = [target for target in self.targets if target.distribution == BASELINE_DISTRIBUTION]
        return sorted(drivers, key=self.get_geometric_mean_time)

    def get_baseline(self) -> Target | None:
        """hare-orm's fastest driver in this report - the summary's ×1; None when no hare-orm
        target ran."""
        drivers = self.get_baseline_drivers()
        return drivers[0] if drivers else None

    def summary_ratio(self, baseline: Target) -> str:
        """How many times slower than ``baseline`` every target is - hare-orm's other drivers
        included - the ratio of their geometric means over every scenario but the cold start."""
        baseline_time = self.get_geometric_mean_time(baseline)
        rows = []
        for target in self.targets:
            ratio = self.get_geometric_mean_time(target) / baseline_time
            # A driver this close to the baseline still shows that it isn't the baseline: ×1.002, not ×1.00.
            decimals = 3 if target is not baseline and round(ratio, 2) == 1 else 2
            rows.append((target, ratio, "×" + self.format_number(ratio, decimals)))
        rows.sort(key=lambda row: row[1])
        if self.language == "ru":
            title = f"Во сколько раз медленнее, чем {baseline.name}"
        else:
            title = f"Times slower than {baseline.name}"
        return self.ranked_bars(title, rows)

    def summary_load(self) -> str:
        """The load test's operations per second."""
        rows = [
            (
                target,
                self.get_median(target, "load_test_throughput_ops_per_sec"),
                self.format_number(self.get_median(target, "load_test_throughput_ops_per_sec"), 0),
            )
            for target in self.targets
        ]
        rows.sort(key=lambda row: -row[1])
        title = "Нагрузка: операций в секунду" if self.language == "ru" else "Load: operations per second"
        return self.ranked_bars(title, rows)

    def group(self, group_key: str) -> str:
        """One scenario group: a legend, then a group of bars per scenario, two columns wide, each
        scenario on its own scale with the time at the end of each bar."""
        __, english_title, russian_title, scenarios = next(group for group in SCENARIO_GROUPS if group[0] == group_key)
        legend_elements, legend_height = self.legend(0)
        elements = list(legend_elements)
        column_gap = 36.0
        column_width = (self.WIDTH - column_gap) / 2
        bar_thickness, bar_gap = 10.0, 4.0
        block_height = 26 + len(self.targets) * (bar_thickness + bar_gap) + 18
        top = legend_height + 14
        for index, (scenario, english_label, russian_label) in enumerate(scenarios):
            column, row = index % 2, index // 2
            x = column * (column_width + column_gap)
            y = top + row * block_height
            label = self.get_label(english_label, russian_label)
            elements.append(self.text(x, y + 14, label, size=13.5, font_weight="600"))
            values = [self.get_median(target, scenario) for target in self.targets]
            maximum, fastest = max(values), min(values)
            plot_width = column_width - 88
            runs = [self.report["targets"][target.key]["scenarios"][scenario]["runs"] for target in self.targets]
            maximum = max(maximum, *(max(target_runs) for target_runs in runs))
            for target_index, (target, value) in enumerate(zip(self.targets, values, strict=True)):
                bar_y = y + 24 + target_index * (bar_thickness + bar_gap)
                length = max(2.0, value / maximum * plot_width)
                elements.append(self.bar(x, bar_y, length, bar_thickness, self.get_series_colour(target)))
                # The spread of the runs: a thin line from the best run to the worst.
                lowest = min(runs[target_index]) / maximum * plot_width
                highest = max(runs[target_index]) / maximum * plot_width
                if highest - lowest >= 2:
                    middle = bar_y + bar_thickness / 2
                    elements.append(
                        f'<line x1="{x + lowest:.1f}" y1="{middle:.1f}" x2="{x + highest:.1f}" y2="{middle:.1f}" '
                        f'stroke="{self.colours["ink"]}" stroke-opacity="0.55" stroke-width="1.5"/>'
                    )
                is_fastest = value == fastest
                elements.append(
                    self.text(
                        x + max(length, highest) + 6,
                        bar_y + 9,
                        self.format_milliseconds(value),
                        size=11,
                        family=self.MONOSPACE,
                        colour=self.colours["ink"] if is_fastest else self.colours["muted"],
                        font_weight="700" if is_fastest else "400",
                    )
                )
        rows = (len(scenarios) + 1) // 2
        title = self.get_label(english_title, russian_title)
        return self.document(top + rows * block_height, elements, title)

    @classmethod
    def write_all(cls, report: dict[str, Any]) -> list[Path]:
        """Writes every chart in every language and colour scheme.

        Args:
            report: The report.

        Returns:
            The files written.
        """
        CHART_DIRECTORY.mkdir(parents=True, exist_ok=True)
        written = []
        for language in ("en", "ru"):
            for theme in ("light", "dark"):
                writer = cls(report, language, theme)
                charts = {"summary-load": writer.summary_load()}
                baseline = writer.get_baseline()
                if baseline is not None:
                    charts["summary-slower"] = writer.summary_ratio(baseline)
                for group_key, __, __, __ in SCENARIO_GROUPS:
                    charts[group_key] = writer.group(group_key)
                for name, svg in charts.items():
                    path = CHART_DIRECTORY / f"{name}-{language}-{theme}.svg"
                    path.write_text(svg, encoding="utf-8")
                    written.append(path)
        return written


class BenchmarkPages:
    """Every text about the benchmark, written from the report and the benchmark's own settings: the
    docs' benchmark pages (whole), the Benchmarks section of both READMEs and the scenario and method
    lists of the benchmark's README (between their markers). Nothing there is edited by hand."""

    DOCS_PAGES = {"en": REPOSITORY / "docs" / "benchmarks.md", "ru": REPOSITORY / "docs" / "benchmarks.ru.md"}
    READMES = {"en": REPOSITORY / "README.md", "ru": REPOSITORY / "README.ru.md"}
    BENCHMARK_READMES = {"en": BENCHMARK_DIRECTORY / "README.md", "ru": BENCHMARK_DIRECTORY / "README.ru.md"}
    SITE_URL = "https://hare-orm.github.io/hare-orm/"
    REPOSITORY_URL = "https://github.com/hare-orm/hare-orm/blob/dev/"
    GENERATED_NOTICE = "<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->"

    def __init__(self, report: dict[str, Any], language: str) -> None:
        """
        Args:
            report: The report of ``Runner.run_all()``.
            language: ``en`` or ``ru``.
        """
        self.report = report
        self.language = language
        self.russian = language == "ru"
        self.charts = ChartWriter(report, language, "light")
        self.counts = Workload.get_counts(report["rows"])
        self.targets = self.charts.targets

    @staticmethod
    def get_russian_plural(number: int, one: str, few: str, many: str) -> str:
        """The Russian noun form for ``number``: 1 задача, 2 задачи, 5 задач."""
        if number % 10 == 1 and number % 100 != 11:
            return one
        if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
            return few
        return many

    @staticmethod
    def get_number(value: int) -> str:
        return str(value)

    @staticmethod
    def wrap(text: str, indent: str = "") -> str:
        """A paragraph or list item broken into lines of at most 100 characters."""
        return textwrap.fill(text, width=100, subsequent_indent=indent, break_long_words=False, break_on_hyphens=False)

    def join(self, items: list[str]) -> str:
        """``a, b and c`` in the page's language."""
        conjunction = " и " if self.russian else " and "
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + conjunction + items[-1]

    def get_compared_names(self) -> str:
        """The other ORMs, one name per package."""
        names: list[str] = []
        for target in self.targets:
            if target.distribution != BASELINE_DISTRIBUTION and target.distribution not in names:
                names.append(target.distribution)
        return self.join(names)

    def get_scenario_count(self) -> int:
        return sum(len(scenarios) for __, __, __, scenarios in SCENARIO_GROUPS)

    def get_baseline_phrase(self) -> str:
        """hare-orm on the run's fastest driver, as the summary sentences name it: "hare-orm on the
        Rust driver (its faster driver in this run)"."""
        drivers = self.charts.get_baseline_drivers()
        baseline = drivers[0]
        if self.russian:
            phrase = f"hare-orm на {baseline.driver_russian}"
            if len(drivers) > 1:
                degree = "более" if len(drivers) == 2 else "самом"
                phrase += f" ({degree} быстром драйвере hare-orm в этом прогоне)"
            return phrase
        phrase = f"hare-orm on {baseline.driver_english}"
        if len(drivers) > 1:
            phrase += f" (its {'faster' if len(drivers) == 2 else 'fastest'} driver in this run)"
        return phrase

    def get_other_drivers_sentence(self) -> str:
        """That hare-orm's other drivers are in the summary too; empty with one driver."""
        others = self.charts.get_baseline_drivers()[1:]
        if not others:
            return ""
        if self.russian:
            names = self.join([f"на {target.driver_russian}" for target in others])
            return f" hare-orm {names} тоже есть в этом сравнении."
        names = self.join([f"on {target.driver_english}" for target in others])
        return f" hare-orm {names} is compared with it too."

    def get_load_description(self) -> str:
        """Who runs the load test and its mix of operations."""
        workers, operations = LOAD_CONCURRENCY, LOAD_OPERATIONS
        if self.russian:
            return (
                f"{self.get_number(workers)} {self.get_russian_plural(workers, 'задача', 'задачи', 'задач')} "
                f"{self.get_russian_plural(workers, 'выполняет', 'выполняют', 'выполняют')} "
                f"{self.get_number(operations)} "
                f"{self.get_russian_plural(operations, 'операцию', 'операции', 'операций')} на одном пуле "
                "подключений: 80% — `get()`, 15% — чтение с фильтром, 5% — `get()` + `save()`"
            )
        return (
            f"{self.get_number(workers)} workers run {self.get_number(operations)} operations on one connection "
            "pool: 80% `get()`, 15% a filtered read, 5% `get()` + `save()`"
        )

    def get_chart_path(self, name: str, theme: str) -> str:
        return f"assets/benchmarks/{name}-{self.language}-{theme}.svg"

    def get_docs_image(self, name: str, alternative_text: str) -> str:
        """Both variants of a chart: the docs theme and GitHub each show the one for their colour scheme."""
        return (
            f"![{alternative_text}]({self.get_chart_path(name, 'light')}#gh-light-mode-only)\n"
            f"![{alternative_text}]({self.get_chart_path(name, 'dark')}#gh-dark-mode-only)"
        )

    def get_readme_image(self, name: str, alternative_text: str) -> str:
        width = ChartWriter.WIDTH + 2 * ChartWriter.PADDING
        return (
            '<p align="center">\n  <picture>\n'
            f'    <source media="(prefers-color-scheme: dark)" srcset="docs/{self.get_chart_path(name, "dark")}">\n'
            f'    <img src="docs/{self.get_chart_path(name, "light")}" alt="{alternative_text}" width="{width}">\n'
            "  </picture>\n</p>"
        )

    def get_slower_alternative_text(self) -> str:
        if self.russian:
            return "Во сколько раз медленнее hare-orm, в среднем"
        return "Times slower than hare-orm, on average"

    def get_load_alternative_text(self) -> str:
        return "Операций в секунду под нагрузкой" if self.russian else "Operations per second under load"

    def get_environment_table(self) -> str:
        """The machine, the versions and the run settings."""
        report, russian = self.report, self.russian
        environment = report["environment"]
        rows = [
            ("Дата" if russian else "Date", report["generated"]),
            (
                "Машина" if russian else "Machine",
                f"{environment['os']}, {environment['processor']}, {environment['cpu_count']} CPU",
            ),
            ("Python", environment["python"]),
            ("PostgreSQL", report["postgresql"]),
            ("asyncpg", report["asyncpg"]),
        ]
        # One row per package: both hare targets are hare-orm, both SQLAlchemy ones SQLAlchemy.
        versions: dict[str, str] = {}
        for key, data in report["targets"].items():
            versions.setdefault(TARGET_BY_KEY[key].distribution, data["version"])
        rows.extend(versions.items())
        if report.get("hare_commit"):
            rows.append(("Коммит hare-orm" if russian else "hare-orm commit", f"`{report['hare_commit']}`"))
        runs = str(report["runs"])
        if report["runs"] > 1:
            runs += ", медиана" if russian else ", median"
        rows.append(("Прогонов" if russian else "Runs", runs))
        rows.append(("Строк в таблице" if russian else "Rows in the table", self.get_number(report["rows"])))
        header = "| | |\n|---|---|\n"
        return header + "\n".join(f"| {name} | {value} |" for name, value in rows)

    def get_method_items(self) -> list[str]:
        """How the numbers are taken - the same list on the docs page and in the benchmark's README."""
        pool = POOL_MAX_SIZE
        has_both_sqlalchemy = {"sqlalchemy", "sqlalchemy-autocommit"} <= set(self.report["targets"])
        if self.russian:
            items = [
                f"У каждой ORM пул из {pool} "
                f"{self.get_russian_plural(pool, 'подключения', 'подключений', 'подключений')}; все они "
                "открываются до начала замеров.",
                "Внутри прогона сценарий повторяется 3–5 раз, и в зачёт идёт самый быстрый повтор; удаления и "
                "`add()` многие-ко-многим меняют данные безвозвратно и выполняются один раз.",
                "Каждая ORM работает в отдельном процессе. Прогоны идут по кругу, в каждом круге ORM "
                "запускаются в случайном порядке; результат сценария — медиана по прогонам.",
            ]
            if has_both_sqlalchemy:
                items.append(
                    "SQLAlchemy измеряется дважды: с транзакцией вокруг каждой сессии (её поведение по "
                    "умолчанию) и с движком в режиме autocommit, где каждая команда фиксируется сразу, как в "
                    "hare и Django."
                )
            items.append("Каждый сценарий написан так, как его пишет документация этой ORM.")
            return items
        items = [
            f"Every ORM gets a pool of {pool} connections, all opened before anything is timed.",
            "Within a run, a scenario is repeated 3–5 times and its fastest repetition counts; the deletes "
            "and the many-to-many `add()` change the data for good and run once.",
            "Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled "
            "order each round; a scenario's result is the median over the runs.",
        ]
        if has_both_sqlalchemy:
            items.append(
                "SQLAlchemy runs twice: with a transaction around each session (its default) and with the "
                "engine in autocommit mode, where every statement commits on its own, as in hare and Django."
            )
        items.append("Every scenario is written the way that ORM's documentation writes it.")
        return items

    def get_docs_page(self) -> str:
        """The whole benchmark page of the docs."""
        russian = self.russian
        scenario_count = self.get_scenario_count()
        if russian:
            intro = (
                f"hare-orm против {self.get_compared_names()} на одних и тех же сценариях (всего "
                f"{scenario_count}): чтение, связи, агрегаты, запись, транзакции и запуск, а также "
                "параллельная нагрузка — с одним сервером PostgreSQL. Чем короче столбец, тем быстрее; "
                "исключение — нагрузка, где считаются операции в секунду."
            )
            summary = (
                f"Во сколько раз каждая ORM в среднем медленнее, чем {self.get_baseline_phrase()}. "
                "Среднее — это отношение средних геометрических времени по всем сценариям, кроме запуска."
                f"{self.get_other_drivers_sentence()}"
            )
            load = f"Нагрузка: {self.get_load_description()}. Операций в секунду:"
            scale = (
                "У каждого сценария своя шкала: сравнивайте столбцы внутри одного сценария, а не между "
                "сценариями. Лучшее время выделено жирным."
            )
            if self.report["runs"] > 1:
                scale += (
                    f" Столбец — медиана {self.report['runs']} прогонов, тонкая линия поверх него — разброс "
                    "от лучшего прогона к худшему."
                )
            headings = ("Производительность", "Сводка", "По сценариям", "Где и как измерялось")
            footer = (
                f"Стенд — [`benchmarks/bench.py`]({self.REPOSITORY_URL}benchmarks/bench.py) в репозитории; "
                f"в его [README]({self.REPOSITORY_URL}benchmarks/README.ru.md) перечислены все сценарии и "
                "описано, как повторить замер на своей машине."
            )
        else:
            intro = (
                f"hare-orm against {self.get_compared_names()} on the same {scenario_count} scenarios — "
                "reads, relations, aggregates, writes, transactions and a cold start — plus a concurrent "
                "load test, against one PostgreSQL server. A shorter bar is faster, except in the load "
                "test, which counts operations per second."
            )
            summary = (
                f"How many times slower than {self.get_baseline_phrase()} each ORM is, on average. The "
                "average is the ratio of geometric means of the times over every scenario except the cold "
                f"start.{self.get_other_drivers_sentence()}"
            )
            load = f"The load test: {self.get_load_description()}. Operations per second:"
            scale = (
                "Each scenario is drawn on its own scale: compare the bars within one scenario, not across "
                "scenarios. The fastest time is in bold."
            )
            if self.report["runs"] > 1:
                scale += (
                    f" A bar is the median of {self.report['runs']} runs, and the thin line over it spans "
                    "the best run to the worst."
                )
            headings = ("Benchmarks", "Summary", "By scenario", "Where and how it was measured")
            footer = (
                f"The benchmark is [`benchmarks/bench.py`]({self.REPOSITORY_URL}benchmarks/bench.py) in the "
                f"repository; its [README]({self.REPOSITORY_URL}benchmarks/README.md) lists every scenario "
                "and how to run it on your own machine."
            )
        sections = [
            self.GENERATED_NOTICE,
            "",
            f"# {headings[0]}",
            "",
            self.wrap(intro),
            "",
            f"## {headings[1]}",
            "",
            self.wrap(summary),
            "",
        ]
        if self.charts.get_baseline() is not None:
            sections += [self.get_docs_image("summary-slower", self.get_slower_alternative_text()), ""]
        sections += [self.wrap(load), "", self.get_docs_image("summary-load", self.get_load_alternative_text()), ""]
        sections += [f"## {headings[2]}", "", self.wrap(scale), ""]
        for group_key, english_title, russian_title, __ in SCENARIO_GROUPS:
            title = self.charts.get_label(english_title, russian_title)
            sections += [f"### {title}", "", self.get_docs_image(group_key, title), ""]
        sections += [f"## {headings[3]}", "", self.get_environment_table(), ""]
        sections += [self.wrap(f"- {item}", "  ") for item in self.get_method_items()]
        sections += ["", self.wrap(footer), ""]
        return "\n".join(sections)

    def get_readme_section(self) -> str:
        """The Benchmarks section of a README, under its heading."""
        page_url = f"{self.SITE_URL}{'ru/' if self.russian else ''}benchmarks/"
        workers = LOAD_CONCURRENCY
        if self.russian:
            intro = (
                f"hare-orm против {self.get_compared_names()} на одних и тех же сценариях (всего "
                f"{self.get_scenario_count()}) с одним сервером PostgreSQL. Во сколько раз каждая ORM в "
                f"среднем медленнее, чем {self.get_baseline_phrase()}, и сколько операций в секунду "
                f"выполняют {self.get_number(workers)} "
                f"{self.get_russian_plural(workers, 'задача', 'задачи', 'задач')} на одном пуле подключений:"
            )
            footer = (
                f"Все сценарии, машина и версии — на странице [Производительность]({page_url}) "
                "документации; сам стенд — [`benchmarks/bench.py`](benchmarks/README.ru.md)."
            )
        else:
            intro = (
                f"hare-orm against {self.get_compared_names()} on the same {self.get_scenario_count()} "
                f"scenarios, against one PostgreSQL server: how many times slower than "
                f"{self.get_baseline_phrase()} each ORM is on average, and how many operations per second "
                f"{self.get_number(workers)} workers get through on one connection pool:"
            )
            footer = (
                f"Every scenario, the machine and the versions are on the [Benchmarks]({page_url}) page; the "
                "benchmark itself is [`benchmarks/bench.py`](benchmarks/README.md)."
            )
        parts = [self.wrap(intro), ""]
        if self.charts.get_baseline() is not None:
            parts.append(self.get_readme_image("summary-slower", self.get_slower_alternative_text()))
        parts += [self.get_readme_image("summary-load", self.get_load_alternative_text()), "", self.wrap(footer)]
        return "\n".join(parts)

    def get_scenario_list(self) -> str:
        """The scenarios by group, as the charts label them, and the load test."""
        rows, small = self.report["rows"], SIZES["small"]
        if self.russian:
            lines = [
                "У каждого сценария одно имя и одна и та же работа во всех ORM; он написан так, как его пишет "
                f"документация этой ORM. В таблице {self.get_number(rows)} "
                f"{self.get_russian_plural(rows, 'виджет', 'виджета', 'виджетов')} (`--size small`: "
                f"{self.get_number(small)}), у каждого — гаджет (внешний ключ), метка (многие-ко-многим) и "
                "документ JSON. Сценарии — так, как они подписаны на графиках:",
            ]
        else:
            lines = [
                "Every scenario has the same name and does the same work in each ORM, written the way that "
                f"ORM's documentation writes it. The table has {self.get_number(rows)} widgets (`--size small`: "
                f"{self.get_number(small)}), each with a gadget (a foreign key), a tag (many-to-many) and a JSON "
                "document. The scenarios, as the charts label them:",
            ]
        lines = [self.wrap(lines[0]), ""]
        for __, english_title, russian_title, scenarios in SCENARIO_GROUPS:
            lines.append(f"- **{self.charts.get_label(english_title, russian_title)}**")
            lines.extend(f"  - {self.charts.get_label(english, russian)}" for __, english, russian in scenarios)
        load_title = "Нагрузка" if self.russian else "Load"
        lines.append(self.wrap(f"- **{load_title}** — {self.get_load_description()}.", "  "))
        return "\n".join(lines)

    def get_method_list(self) -> str:
        return "\n".join(self.wrap(f"- {item}", "  ") for item in self.get_method_items())

    @staticmethod
    def replace_block(path: Path, name: str, content: str) -> None:
        """Replaces the text between ``<!-- name:start -->`` and ``<!-- name:end -->`` in a file.

        Raises:
            ValueError: The file has no such markers.
        """
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        text = path.read_text(encoding="utf-8")
        if start not in text or end not in text:
            raise ValueError(f"{path.relative_to(REPOSITORY)} has no {start} ... {end} block")
        before, rest = text.split(start, 1)
        __, after = rest.split(end, 1)
        path.write_text(f"{before}{start}\n{content}\n{end}{after}", encoding="utf-8", newline="\n")

    @classmethod
    def write_all(cls, report: dict[str, Any]) -> None:
        """Writes every page and block, in both languages."""
        for language in ("en", "ru"):
            pages = cls(report, language)
            cls.DOCS_PAGES[language].write_text(pages.get_docs_page(), encoding="utf-8", newline="\n")
            cls.replace_block(cls.READMES[language], "benchmarks", pages.get_readme_section())
            cls.replace_block(cls.BENCHMARK_READMES[language], "benchmark-scenarios", pages.get_scenario_list())
            cls.replace_block(cls.BENCHMARK_READMES[language], "benchmark-method", pages.get_method_list())


def main() -> None:
    parser = argparse.ArgumentParser(description="hare-orm's comparative ORM benchmark.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", help="One run of one ORM.")
    run_command.add_argument("target", choices=[target.key for target in TARGETS])
    run_command.add_argument("--size", choices=list(SIZES), default="large")
    run_command.add_argument("--port", type=int, default=5433, help="The PostgreSQL server's port at 127.0.0.1.")
    run_command.add_argument("--out", type=Path, help="Writes the run's results to this JSON file.")
    all_command = commands.add_parser("all", help="Every ORM several times, then results.json and the charts.")
    all_command.add_argument("--runs", type=int, default=1)
    all_command.add_argument("--size", choices=list(SIZES), default="large")
    all_command.add_argument("--port", type=int, default=5433)
    all_command.add_argument("--targets", nargs="+", choices=[target.key for target in TARGETS])
    commands.add_parser("charts", help="Draws the charts from results.json again.")
    arguments = parser.parse_args()

    if arguments.command == "run":
        result = asyncio.run(Runner.run_once(TARGET_BY_KEY[arguments.target], arguments.size, arguments.port))
        if arguments.out:
            arguments.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        for name, value in result["results"].items():
            print(f"  {name:36s} {value:12.2f}")
    elif arguments.command == "all":
        targets = [TARGET_BY_KEY[key] for key in arguments.targets] if arguments.targets else list(TARGETS)
        report = Runner.run_all(targets, arguments.runs, arguments.size, arguments.port)
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {RESULTS_FILE.relative_to(REPOSITORY)}")
        ChartWriter.write_all(report)
        BenchmarkPages.write_all(report)
    else:
        report = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        ChartWriter.write_all(report)
        BenchmarkPages.write_all(report)


if __name__ == "__main__":
    main()
