"""The ORM set-ups the benchmark measures, by database. The chart order is the colour order: the
colours are the dataviz reference palette's steps in an order that passes its colour-blind and contrast
checks on both backgrounds, and an ORM keeps its colour on every database."""

from __future__ import annotations

from orm_benchmark.definitions.target import Target

HARE_COLOURS = ("#eb6834", "#d95926")
HARE_ASYNCPG_COLOURS = ("#2a78d6", "#3987e5")
SQLALCHEMY_COLOURS = ("#1baf7a", "#199e70")
SQLALCHEMY_AUTOCOMMIT_COLOURS = ("#eda100", "#c98500")
TORTOISE_COLOURS = ("#e87ba4", "#d55181")
YARA_COLOURS = ("#4a3aa7", "#9085e9")
DJANGO_COLOURS = ("#008300", "#008300")
#: The driver alone, without an ORM - a neutral grey, as a floor rather than a competitor.
DRIVER_COLOURS = ("#7d8494", "#9aa1b0")

TARGETS = (
    # PostgreSQL
    Target(
        "hare-rust",
        "hare (Rust driver)",
        "postgresql",
        "active_record",
        "hare-orm",
        "hare-orm",
        *HARE_COLOURS,
        "the Rust driver",
        "драйвере на Rust",
    ),
    Target(
        "hare-asyncpg",
        "hare (asyncpg)",
        "postgresql",
        "active_record",
        "hare-orm",
        "hare-orm",
        *HARE_ASYNCPG_COLOURS,
        "asyncpg",
        "драйвере asyncpg",
    ),
    Target("sqlalchemy", "SQLAlchemy", "postgresql", "sqlalchemy", "SQLAlchemy", "SQLAlchemy", *SQLALCHEMY_COLOURS),
    Target(
        "sqlalchemy-autocommit",
        "SQLAlchemy (autocommit)",
        "postgresql",
        "sqlalchemy",
        "SQLAlchemy (autocommit)",
        "SQLAlchemy",
        *SQLALCHEMY_AUTOCOMMIT_COLOURS,
    ),
    Target(
        "tortoise", "tortoise-orm", "postgresql", "active_record", "tortoise-orm", "tortoise-orm", *TORTOISE_COLOURS
    ),
    Target("yara-orm", "yara-orm", "postgresql", "active_record", "yara-orm", "yara-orm", *YARA_COLOURS),
    Target("django", "Django", "postgresql", "django", "Django", "Django", *DJANGO_COLOURS),
    # SQLite
    Target(
        "hare-sqlite",
        "hare",
        "sqlite",
        "active_record",
        "hare-orm",
        "hare-orm",
        *HARE_COLOURS,
        "aiosqlite",
        "aiosqlite",
    ),
    Target("sqlalchemy-sqlite", "SQLAlchemy", "sqlite", "sqlalchemy", "SQLAlchemy", "SQLAlchemy", *SQLALCHEMY_COLOURS),
    Target(
        "sqlalchemy-autocommit-sqlite",
        "SQLAlchemy (autocommit)",
        "sqlite",
        "sqlalchemy",
        "SQLAlchemy (autocommit)",
        "SQLAlchemy",
        *SQLALCHEMY_AUTOCOMMIT_COLOURS,
    ),
    Target(
        "tortoise-sqlite", "tortoise-orm", "sqlite", "active_record", "tortoise-orm", "tortoise-orm", *TORTOISE_COLOURS
    ),
    Target("yara-orm-sqlite", "yara-orm", "sqlite", "active_record", "yara-orm", "yara-orm", *YARA_COLOURS),
    Target("django-sqlite", "Django", "sqlite", "django", "Django", "Django", *DJANGO_COLOURS),
    # ClickHouse
    Target(
        "hare-clickhouse",
        "hare (clickhouse-connect)",
        "clickhouse",
        "hare_clickhouse",
        "hare-orm",
        "hare-orm",
        *HARE_COLOURS,
        "clickhouse-connect over HTTP",
        "драйвере clickhouse-connect по HTTP",
    ),
    Target(
        "hare-clickhouse-driver",
        "hare (clickhouse-driver)",
        "clickhouse",
        "hare_clickhouse",
        "hare-orm",
        "hare-orm",
        *HARE_ASYNCPG_COLOURS,
        "clickhouse-driver over the native TCP protocol",
        "драйвере clickhouse-driver по родному протоколу TCP",
    ),
    Target(
        "clickhouse-connect",
        "clickhouse-connect (no ORM)",
        "clickhouse",
        "clickhouse_connect",
        "clickhouse-connect",
        "clickhouse-connect",
        *DRIVER_COLOURS,
    ),
    Target(
        "sqlalchemy-clickhouse",
        "SQLAlchemy",
        "clickhouse",
        "sqlalchemy_clickhouse",
        "SQLAlchemy",
        "clickhouse-sqlalchemy",
        *SQLALCHEMY_COLOURS,
        environment="clickhouse-sqlalchemy",
    ),
    Target(
        "django-clickhouse",
        "Django",
        "clickhouse",
        "django_clickhouse",
        "Django",
        "django-clickhouse-backend",
        *DJANGO_COLOURS,
    ),
)
TARGET_BY_KEY = {target.key: target for target in TARGETS}
