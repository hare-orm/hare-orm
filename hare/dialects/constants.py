from __future__ import annotations

#: The entry point group whose entries name modules registering third-party drivers on import.
DIALECT_ENTRY_POINT_GROUP = "hare.dialects"

#: hare's own dialects by name, as ``(module, attribute)`` of each dialect object. Each one is
#: registered when first used - a connection to one database loads no other dialect - and they
#: are listed first, in this order, among all dialects.
BUILTIN_DIALECTS_BY_NAME = {
    "sql": ("hare.dialects.base.constants", "SQL_DIALECT"),
    "sqlite": ("hare.dialects.sqlite.constants", "SQLITE_DIALECT"),
    "postgresql": ("hare.dialects.postgresql.constants", "POSTGRESQL_DIALECT"),
    "clickhouse": ("hare.dialects.clickhouse.constants", "CLICKHOUSE_DIALECT"),
}
#: The module registering each of hare's own drivers on import, by each DB_URL scheme of the driver -
#: its name among them. Only the module of a driver a connection uses is imported. A driver's scheme
#: names the driver after the dialect; the plain dialect scheme belongs to hare's own engine of the
#: dialect (``postgresql://``), and no driver of the other dialects takes it.
BUILTIN_DRIVER_MODULES_BY_NAME = {
    "sqlite+aiosqlite": "hare.dialects.sqlite.drivers.aiosqlite.aiosqlite_driver",
    "postgresql+asyncpg": "hare.dialects.postgresql.drivers.asyncpg.asyncpg_driver",
    "postgresql": "hare.dialects.postgresql.drivers.rust_pg.rust_pg_driver",
    "clickhouse+clickhouse-connect": "hare.dialects.clickhouse.drivers.clickhouse_connect.clickhouse_connect_driver",
    "clickhouse+clickhouse-driver": "hare.dialects.clickhouse.drivers.clickhouse_driver.clickhouse_driver_driver",
}

#: Bind parameters one statement may carry unless the dialect or its driver says otherwise - a
#: count that fits a signed 16-bit integer, the narrowest a wire protocol counts them in.
DEFAULT_MAX_BIND_PARAMETERS = 32767
