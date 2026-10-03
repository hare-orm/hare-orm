#: The entry point group whose entries name modules registering third-party drivers on import.
DIALECT_ENTRY_POINT_GROUP = "hare.dialects"

#: hare's own dialects by name, as ``(module, attribute)`` of each dialect object. Each one is
#: registered when first used - a connection to one database loads no other dialect - and they
#: are listed first, in this order, among all dialects.
BUILTIN_DIALECTS_BY_NAME = {
    "sql": ("hare.dialects.base.constants", "SQL_DIALECT"),
    "sqlite": ("hare.dialects.sqlite.constants", "SQLITE_DIALECT"),
    "postgresql": ("hare.dialects.postgresql.constants", "POSTGRESQL_DIALECT"),
}
#: The module registering each of hare's own drivers on import, by driver name - which is also the
#: driver's one DB_URL scheme. Only the module of a driver a connection uses is imported.
BUILTIN_DRIVER_MODULES_BY_NAME = {
    "sqlite": "hare.dialects.sqlite.driver",
    "postgresql+asyncpg": "hare.dialects.postgresql.drivers.asyncpg.driver",
    "postgresql": "hare.dialects.postgresql.drivers.rust_pg.driver",
}
#: The most bytes a name hare generates - a table, column, through table, index, constraint or
#: alias name - takes: PostgreSQL's limit, the shortest among hare's own dialects. A longer name is
#: shortened with a digest (``Identifiers``). The limit is fixed rather than taken from the
#: dialects loaded, so a model gets the same names on every connection and every run; a dialect
#: keeping names shorter than this can't be registered.
IDENTIFIER_LENGTH_LIMIT = 63

#: The oldest PostgreSQL server hare runs on.
POSTGRESQL_MINIMUM_SERVER_VERSION = (14,)
#: The first PostgreSQL version whose unique constraints take ``NULLS [NOT] DISTINCT``.
POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION = (15,)
#: The first PostgreSQL version whose partitioned tables take an exclusion constraint.
POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION = (17,)
#: The oldest SQLite library hare runs on - ``RETURNING`` and ``ALTER TABLE ... DROP COLUMN`` first
#: appeared in 3.35.0.
SQLITE_MINIMUM_SERVER_VERSION = (3, 35, 0)

#: The list length from which `__in`/`__not_in` binds the list as one JSON array parameter (`IN
#: (SELECT value FROM json_each(?))`) - one statement plan for every longer list, and no
#: bind-parameter ceiling.
SQLITE_IN_JSON_ARRAY_THRESHOLD = 20

#: A rough cutoff: `= ANY($1)` beat `IN (...)` by ~9% at 500 values and was no better at 3-10.
POSTGRES_IN_ARRAY_THRESHOLD = 20

#: How many hex characters of a SHA-256 digest end a generated name shortened to fit the
#: identifier length limit - 40 bits, leaving collisions between shortened names practically
#: unreachable.
IDENTIFIER_DIGEST_LENGTH = 10

#: How many shortened identifiers ``Identifiers.shorten()`` keeps.
SHORTENED_IDENTIFIER_CACHE_SIZE = 1024
