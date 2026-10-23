# Dialects and features

A **dialect** is the SQL language, column types, DDL and catalog of one type of database; a **driver** is
how hare connects to it. One dialect can have several drivers — PostgreSQL has two. A connection's
`engine` (or its database URL scheme — the same word) picks the driver, and with it the dialect. See
[PostgreSQL: the drivers](postgresql/connecting.md#choosing-a-postgres-driver).

| Dialect | `engine` / database URL scheme | Driver | Oldest server |
|---|---|---|---|
| `sqlite` | `sqlite+aiosqlite` | `aiosqlite` over the standard `sqlite3` module | SQLite 3.35.5 |
| `postgresql` | `postgresql` | `rust_pg`, hare's driver written in Rust | PostgreSQL 14 |
| `postgresql` | `postgresql+asyncpg` | `asyncpg` (`hare-orm[asyncpg]`) | PostgreSQL 14 |
| `clickhouse` | `clickhouse+clickhouse-connect` | clickhouse-connect, over HTTP (`hare-orm[clickhouse-connect]`) — [ClickHouse](clickhouse/connecting.md) | ClickHouse 25.8 |
| `clickhouse` | `clickhouse+clickhouse-driver` | clickhouse-driver, over the native TCP protocol (`hare-orm[clickhouse-driver]`) — [ClickHouse](clickhouse/connecting.md) | ClickHouse 25.8 |

`aiosqlite` runs each SQLite connection on a thread of its own, and that thread doesn't keep the
interpreter alive: a program that ends with a connection still open — on an exception, say — exits
instead of hanging, and SQLite's journal rolls an unfinished transaction back.

Everything hare does differently per database — SQL syntax, column types, how a value is written and
read, DDL, reading an existing schema — is decided by the connection's dialect and its `Features`, never
by comparing database names. A query runs in the dialect of the connection it runs on (`using()`, a
transaction, a router or the model's default connection), so one model can be read from SQLite and
PostgreSQL in the same process.

## <a id="database-pages"></a>The pages of each database

What one database does on its own is described in its folder:

- **SQLite:** [Connecting](sqlite/connecting.md), [Functions](sqlite/functions.md),
  [What runs differently](sqlite/differences.md).
- **PostgreSQL:** [Connecting](postgresql/connecting.md), [Fields](postgresql/fields.md),
  [Indexes and tables](postgresql/indexes-and-tables.md), [Functions](postgresql/functions.md),
  [Trigram similarity and unaccent](postgresql/trigrams.md),
  [LISTEN/NOTIFY](postgresql/listen-notify.md), [What runs differently](postgresql/differences.md),
  [Testing](postgresql/testing.md).
- **ClickHouse:** [Connecting](clickhouse/connecting.md), [Models](clickhouse/models.md),
  [Types](clickhouse/types.md), [Query modifiers](clickhouse/query-modifiers.md),
  [Aggregates](clickhouse/aggregates.md), [Schema objects](clickhouse/schema-objects.md),
  [Transactions and locks](clickhouse/transactions-and-locks.md),
  [What runs differently](clickhouse/differences.md), [Testing](clickhouse/testing.md).

## <a id="the-server-version"></a>The server version

Right after a connection opens, hare reads the server's version — without an observer seeing it (the
PostgreSQL drivers read `server_version_num`, SQLite reports its library version). A server older than
its dialect's `minimum_server_version` is refused before anything else runs, and the connection is
closed:

```text
UnSupportedError: The postgresql server is version 13.9, older than 14, the oldest hare runs on
```

A feature that exists only from some version on follows the server's own version: a PostgreSQL 14
connection has `features.supports_nulls_distinct` off, so a `UniqueConstraint(..., nulls_distinct=False)`
on it raises `UnSupportedError` before any DDL is sent. `Hare.generate_schemas()` connects before it
writes the DDL for the same reason.

## <a id="connection-dialect-and-features"></a>`connection.dialect` and `connection.features`

```python
connection = Book.get_connection()
connection.dialect.name                        # "postgresql"
connection.dialect.features.supports_copy      # True
connection.features.supports_nulls_distinct    # True on PostgreSQL 15 and later
```

`connection.dialect` is the dialect object, one per dialect, shared by every connection that speaks it;
`connection.dialect.features` is what the database supports, the same for every connection to it.
`connection.features` is a frozen `Features` of this connection — the dialect's, with what its driver
adds and what its server version changes, fixed once it has connected. hare reads `connection.features`
wherever it has a connection.

### <a id="features"></a>`Features`

| Field | Meaning | `sqlite` | `postgresql` | `postgresql+asyncpg` | `clickhouse` |
|---|---|---|---|---|---|
| `supports_transactions` | Transactions | yes | yes | yes | with `transactions=true` — see [Transactions and locks](clickhouse/transactions-and-locks.md) |
| `can_rollback_ddl` | DDL runs inside a transaction and rolls back with it | yes | yes | yes | no |
| `supports_select_for_update` | `SELECT ... FOR UPDATE` | no | yes | yes | no |
| `supports_select_for_no_key_update` | `SELECT ... FOR NO KEY UPDATE` | no | yes | yes | no |
| `supports_select_for_share` | `SELECT ... FOR SHARE` | no | yes | yes | no |
| `supports_select_for_key_share` | `SELECT ... FOR KEY SHARE` | no | yes | yes | no |
| `supports_update_limit_order_by` | `UPDATE`/`DELETE` with `ORDER BY` and `LIMIT` | no | no | no | no |
| `supports_posix_regex` | POSIX regular expression lookups | with `install_regexp_functions=True` | yes | yes | no |
| `supports_returning` | `INSERT ... RETURNING` | yes | yes | yes | no |
| `reports_written_row_count` | The database reports how many rows a write changed; without, `update()`, `delete()` and `bulk_update()` return `None`, a write of a row that is gone goes unnoticed, and a model with `Meta.optimistic_lock_field` is refused when it is bound | yes | yes | yes | no |
| `supports_insert_conflicts` | `INSERT ... ON CONFLICT` — `bulk_create(ignore_conflicts=..., update_fields=..., on_conflict=..., on_conflict_constraint=...)`; without, those options are refused | yes | yes | yes | no |
| `supports_two_phase_commit` | `PREPARE TRANSACTION` / `COMMIT PREPARED` | no | yes | yes | no |
| `supports_listen_notify` | `LISTEN` / `NOTIFY` | no | yes | yes | no |
| `inline_comments` | Table and column comments go inside `CREATE TABLE` | yes | no | no | yes |
| `supports_positional_rows` | The rows of `execute(rows_by_position=True)` read by position | yes | yes | yes | yes |
| `supports_streaming` | `QuerySet.stream()` pages rows off a cursor inside a transaction | yes | yes | yes | yes |
| `streams_without_transaction` | `stream()` reads rows outside a transaction too — the server sends the rows as it computes them | no | no | no | yes |
| `execute_many_scales_poorly` | `executemany()` is slower than one multi-row statement | no | yes | no | yes |
| `binds_written_parameters` | The driver binds the rows hare's native writer builds as they are | no | yes | no | no |
| `max_bind_parameters` | Most bind parameters one statement may carry | 32766 | 32767 | 32767 | 1000000 |
| `max_expression_depth` | The deepest expression one statement may hold (`None`: no limit) - a long list of OR-ed key groups is split across statements | 1000 | `None` | `None` | `None` |
| `max_bulk_update_rows` | Most rows one `bulk_update()` statement writes (`None`: as many as the parameters allow) | `None` | `None` | `None` | 1000 |
| `cascade_depth_limit` | The recursion depth a native `ON DELETE CASCADE` stops at; the client then raises `CascadeDepthLimitError`, and hare finishes a deep cascade itself, fewer levels than this at a time | the build's `SQLITE_LIMIT_TRIGGER_DEPTH` (1000; 100 in the official 3.37.2 library) | none | none | none |
| `supports_nulls_distinct` | A unique constraint takes `NULLS [NOT] DISTINCT` | no | from 15 | from 15 | no |
| `supports_partitioned_exclusion_constraints` | A partitioned table takes an exclusion constraint | no | from 17 | from 17 | no |
| `supports_unhex` | The SQL function `unhex()` — a long `__in` of bytes binds as one JSON array | from 3.41.0 | no | no | no |
| `supports_drop_column` | `ALTER TABLE ... DROP COLUMN` — `RemoveField` of a plain column drops it in place instead of rebuilding the table | yes | yes | yes | yes |
| `explain_options` | The options `explain(**options)` takes | none | those of the server's version (`GENERIC_PLAN` from 16, `MEMORY` and `SERIALIZE` from 17) | the same | none |
| `supports_pool_status` | The client reports its pool's status and metrics — `get_pool_status()`, [pool metrics](../observability/pool-health.md) | yes | yes | yes | clickhouse-driver — yes, clickhouse-connect — no |

Where a feature is missing, the operation that needs it raises `UnSupportedError` naming it before
anything is sent — `select_for_update()` on SQLite, `Transactions.distributed()` without two-phase
commit, `stream()` without streaming.

### <a id="database-features"></a>Features of the database

The rest of `Features` are the database's own: its dialect declares them, and every driver of it has the
same values.

| Field | Meaning | `sqlite` | `postgresql` | `clickhouse` |
|---|---|---|---|---|
| `supports_schemas` | Migrations create and drop the schemas of `Meta.schema` (`CREATE SCHEMA`); without, a model's schema is written as it is and has to exist — an attached database on SQLite (see [`attached_databases`](sqlite/connecting.md#attached-databases)), a database of the server on ClickHouse | no | yes | no |
| `references_other_schemas` | A foreign key references a table of another schema; without, a relation with `db_constraint=True` to a model of another schema is refused when the model is bound | no | yes | yes |
| `supports_distinct_on` | `SELECT DISTINCT ON (...)` — `distinct("field")` runs it; without, `distinct("field")` is refused | no | yes | no |
| `supports_grouping_sets` | `GROUP BY ROLLUP/CUBE/GROUPING SETS` and `GROUPING()` — `group_by(Rollup(...))`, `Grouping()` | no | yes | no |
| `supports_lateral` | A `LATERAL` subquery in `FROM` — `Lateral(queryset)` | no | yes | no |
| `supports_table_sample` | A sample of a table in `FROM` — `sample()`: `TABLESAMPLE`, ClickHouse's `SAMPLE` | no | yes | yes |
| `supports_asof_join` | `ASOF LEFT JOIN` — `AsofJoin(...)` | no | no | yes |
| `supports_array_join` | Each row repeated with each element of an array of it — `ArrayJoin(...)` | no | no | yes |
| `supports_merge` | `MERGE` — `merge()` | no | yes (15+) | no |
| `supports_merge_returning` | `MERGE ... RETURNING` — `merge().returning()` | no | yes (17+) | no |
| `supports_merge_not_matched_by_source` | `WHEN NOT MATCHED BY SOURCE` — `when_not_matched_by_source()` | no | yes (17+) | no |
| `supports_enum_types` | `CREATE TYPE ... AS ENUM` — `NativeEnumField` | no | yes | no |
| `supports_views` | `Meta.views` and the view operations — see [Views, functions, sequences and access](../models/schema-objects.md) | no | yes | yes |
| `supports_materialized_views` | `Meta.materialized_views`, `RefreshMaterializedView`, `refresh_materialized_view()` | no | yes | yes — see [Schema objects](clickhouse/schema-objects.md) |
| `supports_dictionaries` | `Meta.dictionaries` — rows of a table kept loaded for lookups by key, `DictGet(...)` | no | no | yes |
| `supports_database_functions` | `Meta.functions` and the function operations | no | yes | no |
| `supports_sequences` | `Meta.sequences`, the sequence operations, `get_next_sequence_value()` | no | yes | no |
| `supports_row_level_security` | `Meta.row_level_security`, `Meta.policies` and their operations | no | yes | no |
| `supports_grants` | `Meta.grants`, `AddGrant`/`RemoveGrant` | no | yes | no |
| `sorts_nulls_first` | NULL sorts before every value in ascending order by default | yes | no | no |
| `guarantees_returning_order` | A multi-row `INSERT ... RETURNING` returns rows in insert order — `bulk_create(returning=True)` | no | yes | no |
| `divides_integers_exactly` | `/` of two integers gives the exact quotient, a fraction too - the expression's output field is then a `FloatField` | no | no | yes |
| `orders_json_values_in_aggregates` | `Max()`/`Min()` of a JSON value compare it as the database orders it; without, they are refused | yes | yes | no |
| `supports_uuid_v7` | A `db_default` generating a time-ordered version 7 UUID (`UuidV7()`) | no | from 18 | no |
| `supports_conflict_constraint_names` | `bulk_create(on_conflict_constraint=...)` | no | yes | no |
| `supports_conflict_where` | `bulk_create(conflict_where=...)` | no | yes | no |
| `supports_copy` | A bulk load of rows outside SQL text — `bulk_create(use_copy=True)`: the `COPY` protocol on PostgreSQL, a binary insert on ClickHouse | no | yes | yes |
| `copies_bulk_inserts` | `bulk_create()` loads its rows that way without `use_copy=True`, whenever it handles no conflict and reads no row back | no | no | yes |
| `supports_virtual_generated_columns` | A generated column can be computed on read (`stored=False`); without, a `VIRTUAL` column raises `UnSupportedError` before the DDL | yes | yes (18+) | yes (`ALIAS`) |
| `supports_without_overlaps` | `UniqueConstraint(without_overlaps=True)` and `CompositePrimaryKey(without_overlaps=True)` — `WITHOUT OVERLAPS` on the last field, a range | no | yes (18+) | no |
| `supports_returning_old_new` | `returning(old=...)` of `update()` and `merge()` — `RETURNING old.column` | no | yes (18+) | no |
| `supports_json_table` | `JSON_TABLE` — `JsonTable(...)` | no | yes (17+) | no |
| `supports_strict_tables` | `SqliteTableOptions(strict=True)` — `STRICT` tables | yes (3.37+) | no | no |
| `supports_text_search_configurations` | [Full-text search](search-and-geodata/full-text-search.md) with text search configurations, `SearchVector` values, lexemes, label weights, rank normalization and the headline's fragment options | no | yes | no |
| `supports_full_text_index` | `FullTextIndex` — an FTS5 table kept in step by triggers, which [full-text search](search-and-geodata/full-text-search.md) reads, and `SearchRank`'s weights by field | yes (a SQLite built with FTS5) | no (tsvector search) | no |
| `supports_vector_search` | The distances and `__nearby` of [`hare.vectors`](search-and-geodata/vector-search.md) — pgvector on PostgreSQL, sqlite-vec on SQLite | with `load_sqlite_vec=true` | yes (pgvector) | no |
| `supports_tenant_schemas` | A [schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md) — `tenant_schema_template`, `Meta.tenant_schema`, `TenantSchemas` | no | yes | no |
| `supports_spatial` | The spatial lookups, paths, functions and aggregates of [`hare.gis`](search-and-geodata/gis.md) | with `load_spatialite=true` | yes (PostGIS) | yes (the geo types — see [Types](clickhouse/types.md#geo)) |
| `supports_geography` | A `GeometryField(geography=True)` measured on the ellipsoid | with SpatiaLite's spatial metadata (`spatialite_metadata` other than `none`) | yes | yes (measured on a sphere) |
| `supports_spatial_index` | [`SpatialiteIndex`](search-and-geodata/gis.md#spatial-index) — SpatiaLite's R*Tree, and the spatial lookups narrowed through it | with SpatiaLite's spatial metadata | no (`GistIndex`) | no |
| `spatial_reference_ids` | The SRIDs of the spatial metadata a geography and a spatial index take — `None` for any | the metadata's, read when the connection opens | `None` | `None` |
| `supports_ordered_aggregates` | `ORDER BY` inside an aggregate — `MakeLine(order_by=...)` | yes (3.44+) | yes | no |
| `isolation_levels` | The isolation levels a transaction runs at — see [Isolation level](../connections/transactions.md#isolation-level) | `SERIALIZABLE` | all four | `REPEATABLE READ` — the snapshot the transaction began with |
| `max_identifier_length` | The most bytes a name may take. hare keeps every name it generates within 63 bytes, shortening a longer one with a hash, and refuses to register a dialect with a lower limit | none | 63 | none |
| `creates_tables_with_forward_references` | A table is created with a foreign key to a table not created yet — a cycle of foreign keys is created table by table, not broken | yes | no | no |
| `supports_partial_indexes` | An index takes a `WHERE` condition | yes | yes | no |
| `supports_correlated_aggregates` | A correlated subquery selecting `Count`/`Sum` counts each row once — see [Correlated subqueries](clickhouse/differences.md#correlated-subqueries) | yes | yes | no |
| `cuts_like_pattern_at_null_byte` | `LIKE` reads a pattern up to its first null byte, so a pattern holding one is refused | yes | no | no |
| `supports_deferrable_constraints` | `deferrable=True` on a constraint or trigger | no | yes | no |
| `supports_index_key_order` | An index key sorts ascending or descending | yes | yes | no |
| `collates_in_ordering_only` | A collation (`Collate()`) is taken in an `ORDER BY` key alone - a `distinct()` selects the value and orders it collated | no | no | yes |
| `supports_index_nulls_order` | An index key sets where NULLs sort | no | yes | no |
| `supports_concurrent_indexes` | `CONCURRENTLY` index operations | no | yes | no |
| `supports_not_valid_constraints` | A constraint added `NOT VALID` and validated later | no | yes | no |
| `supports_extensions` | `CREATE EXTENSION` | no | yes | no |
| `supports_collations` | `CREATE COLLATION` | no | yes | no |
| `supports_foreign_keys` | Foreign keys are enforced; without, hare runs every `on_delete` action itself, and a relation with `db_constraint=True` is refused when its model is bound — see [Models](clickhouse/models.md#uniqueness-and-relations) | yes | yes | no |
| `supports_unique_constraints` | Uniqueness is enforced; without, a model declaring one (`unique=True`, `UniqueConstraint`, `Index(unique=True)`) is refused when it is bound | yes | yes | no |
| `supports_recursive_union_distinct` | A recursive `WITH` query adds its rows by `UNION`, each row once; without, a cascade down a tree of rows of one model is walked level by level | yes | yes | no |
| `table_alias_hides_name` | A table read under an alias answers to the alias alone; without, a subquery over the table its enclosing query reads takes its rows from a derived table | yes | yes | no |
| `truncates_values_on_type_change` | A narrowing type change cuts values silently; hare checks the data first | no | yes | yes |
| `alters_indexed_columns` | A column an index covers changes its type or nullability; without, hare drops the covering indexes around the change and creates them again | yes | yes | no |
| `binds_array_parameters` | A list binds as one array parameter (`RawSQL("... = ANY(%s)", [ids])`) | no | yes | no |
| `matches_ordering_to_grouping_by_sql` | An ordering term also grouped by is written exactly as in `GROUP BY` | no | yes | no |
| `checks_foreign_keys_per_cascade_step` | A `NO ACTION` foreign key is checked after every nested step of an `ON DELETE CASCADE`, not once at the end of the statement — a `DELETE` then fails on a row an `on_delete=PROTECT` relation guards even when the same cascade removes the guarding row later, unless the PROTECT constraints are deferred | no | yes | no |
| `checks_restrict_at_statement_end` | A `RESTRICT` foreign key is checked at the end of the statement like `NO ACTION`, not at once before the statement's own cascade goes on | no | no | no |
| `supports_savepoints` | Savepoints — a nested `atomic()` | yes | yes | no |
| `supports_generated_keys` | The database generates a primary key (`IntField(primary_key=True)`); without, such a model is refused when it is bound | yes | yes | with a Keeper |
| `takes_keys_before_insert` | The keys are taken from a series before the rows are written — `generateSerialID` | no | no | yes |
| `supports_row_updates` | An `UPDATE` of stored rows | yes | yes | yes |
| `supports_row_deletes` | A `DELETE` of stored rows | yes | yes | yes |
| `supports_recursive_queries` | A recursive `WITH` query — `with_recursive()`; without, it raises `UnSupportedError` | yes | yes | yes |
| `supports_json_type` | A JSON value is stored by a type of its own, its paths typed — a `JSONField` is a `JSON` column | no | no | yes (`String` over clickhouse-driver) |
| `reads_trailing_comment_of_insert` | A comment ending an `INSERT ... VALUES` is a comment; without, query tags go before such a statement — ClickHouse reads what follows `VALUES` as data | yes | yes | no |
| `supports_correlated_subqueries` | A correlated subquery; without, an `EXISTS` correlated by equal columns is written as an `IN` test and any other is refused | yes | yes | yes |
| `rewrites_correlated_exists` | An `EXISTS` correlated by equal columns (a row of them for a composite key too) is written as an `IN` test even where correlated subqueries run — the server refuses its own in a join's `ON` and over a `UNION DISTINCT`; one correlated by more than equalities is the server's own there, and raises `UnSupportedError` elsewhere | no | no | yes |
| `supports_ordered_correlated_subqueries` | A correlated subquery orders and slices its own rows (`Subquery(... .order_by(...)[:1])`) | yes | yes | no |
| `orders_by_correlated_subqueries` | A correlated subquery runs in `ORDER BY` and beside a `WHERE`; without, the rows are picked in a derived table and filtered, ordered and sliced outside it | yes | yes | no |

Each field is documented on `hare.dialects.base.features.Features`.
`connection.dialect.minimum_server_version` is the oldest server hare runs on: SQLite 3.35.5, PostgreSQL
14, ClickHouse 25.8. How a dialect stores a table — SQLite's `WITHOUT ROWID`, PostgreSQL's tablespace,
`UNLOGGED`, storage parameters and [partitioning](postgresql/indexes-and-tables.md#partitioning) (hash,
list and range partitions, added and removed by migrations) — is set per model with
[`Meta.table_options`](../models/meta-options.md#table_options).

## <a id="dialectregistry"></a>`DialectRegistry`

```python
from hare.dialects.dialect_registry import DialectRegistry

DialectRegistry.get_dialect("postgresql")                  # the dialect object
DialectRegistry.get_driver("postgresql+asyncpg")           # the driver an engine names
DialectRegistry.get_driver_for_url_scheme("sqlite")        # the driver a database URL scheme picks
DialectRegistry.get_dialects(), DialectRegistry.get_drivers()
```

An unknown name or scheme raises `ConfigurationError` listing the registered ones. A built-in dialect or
driver is imported and registered only when a connection or a lookup names it, so `Hare.init()` on
`sqlite+aiosqlite://` imports nothing of PostgreSQL and `Hare.init()` on `postgresql://` nothing of
SQLite; `get_dialects()` lists the built-in dialects first, in the same order every time. A name or
scheme no built-in driver has, and a call to `get_dialects()`/`get_drivers()`, loads every module named
in the `hare.dialects` entry point group of an installed package — a third-party dialect registers its
drivers on import:

```toml
[project.entry-points."hare.dialects"]
colstore = "hare_colstore.driver"
```

Its engine and database URL scheme then work like the built-in ones. How to write one is in
[Writing a dialect](../extending/writing-a-dialect/index.md).

### <a id="registering-after-init"></a>Registering after `Hare.init()`

A dialect, a driver, a term renderer or type mapping of a dialect, a `register_lookup()` lookup, a path
transform and a dialect's QuerySet method can be registered at any time. Every registration drops what
hare cached from the registries — the SQL of queries, row-reading plans, filter and ordering descriptions
— and rebuilds the filters of every bound model, so the next query uses it; a query already built runs on
with the SQL it has. Registrations usually run on import, before `Hare.init()`, when nothing is cached
yet and the drop costs nothing.

## <a id="tests-by-capability"></a>Tests by capability

[`requires_features()`](../testing/setup.md#requires-features) skips a test unless the connection has
what it needs — a field of `Features`, a dialect attribute, or the dialect's name:

```python
@requires_features(supports_distinct_on=True)
async def test_latest_per_author(hare_db: HareContext) -> None: ...
```
