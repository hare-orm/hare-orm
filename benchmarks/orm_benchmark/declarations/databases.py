"""The databases the benchmark runs on."""

from __future__ import annotations

from orm_benchmark.declarations.scenario_groups import CLICKHOUSE_GROUPS, ROW_STORE_GROUPS
from orm_benchmark.definitions.database import Database

ROW_STORE_LOAD_ENGLISH = (
    "{workers} workers run {operations} operations on one connection pool: 80% `get()`, 15% a filtered "
    "read, 5% `get()` + `save()`; the write load runs half `get()` + `save()`"
)
ROW_STORE_LOAD_RUSSIAN = (
    "{workers} задач выполняют {operations} операций на одном пуле подключений: 80% — `get()`, 15% — "
    "чтение с фильтром, 5% — `get()` + `save()`; в нагрузке с записью половина операций — `get()` + `save()`"
)

DATABASES = (
    Database(
        "postgresql", "PostgreSQL", ROW_STORE_GROUPS, frozenset(), ROW_STORE_LOAD_ENGLISH, ROW_STORE_LOAD_RUSSIAN
    ),
    # SQLite has no row locks.
    Database(
        "sqlite",
        "SQLite",
        ROW_STORE_GROUPS,
        frozenset({"select_for_update_loop"}),
        ROW_STORE_LOAD_ENGLISH,
        ROW_STORE_LOAD_RUSSIAN,
    ),
    Database(
        "clickhouse",
        "ClickHouse",
        CLICKHOUSE_GROUPS,
        frozenset(),
        "{workers} workers run {operations} operations: 80% `get()` by key, 20% a `GROUP BY` of a day",
        "{workers} задач выполняют {operations} операций: 80% — `get()` по ключу, 20% — `GROUP BY` за день",
    ),
)
DATABASE_BY_KEY = {database.key: database for database in DATABASES}
