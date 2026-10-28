# Changelog

[Русская версия](CHANGELOG.ru.md)

Every user-visible change to hare-orm is recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) as described in
[Versioning and releases](README.md#versioning-and-releases).

Each release lists its changes in these groups, in this order, leaving out empty ones:

- **Breaking changes** - public API that was removed or renamed, or that now behaves differently.
  Each entry says what to change in your code.
- **Added** - new features and new public API.
- **Changed** - changed behavior that needs no change in your code.
- **Fixed** - bug fixes.
- **Removed** - features dropped without a replacement.
- **Security** - fixed vulnerabilities, with their advisory links.

Changes merged into `dev` since the last release are collected under **Unreleased**, and a release
renames that section to its version and date.

## [Unreleased]

### Breaking changes

- `VersionedModel.id` and `OutboxEvent.id` require UUID7, including values passed through filters and
  relations. Generate these identifiers with `Uuid7.new`; put `VersionedModel` first when combining it
  with a base model declaring an `id` field.
- `makemigrations --rename` refuses an answer that renames nothing instead of writing a migration
  that removes and recreates the object. Correct the names before generating the migration.

### Added

- `UUIDField(version=...)` restricts UUID values to the declared version.
- `HareDTO` supports `extra_fields`, `computed` and `related_values`; `pydantic_model_creator()` supports
  `extra_fields` and `related_values`, including declarations on nested related models.
- Model-specific `Tenancy.scope(...)` works with tenant schemas and row-level security policies.
- Migration safety checks report a required field added without a Python or database default.

### Fixed

- Subquery membership handles NULL values and wrapped model keys consistently. Filters through
  relations bind each subquery's current slice when reusing a plan. Count, existence and set operations
  preserve joins and distinct columns introduced by ordering; grouped summaries expand composite keys.
  Recursive queries declared before initialization are validated when their calls are replayed.
- Composite relation aliases retain key operations, and their null lookups require boolean values.
  Existence checks reject inflated aggregates used by HAVING. Explicit bulk update returning is
  refused before SQL on databases without RETURNING.
- Iterators and cursors preserve duplicate rows, keyless queries, unordered distinct values and
  database-specific annotations. Bounds use the stored field representation, including Float32,
  enum labels, JSON and explicit NULL ordering around NaN.
- Sliced prefetches preserve partial field masks, composite keys, joins, duplicate rows and connection
  routing. Nested prefetches keep the required keys on intermediate models selected by `only()` and
  report missing keys on partial instances with `QueryError`.
- Query annotations remain distinct from similarly named relation paths and internal columns,
  including case differences and long identifiers. Grouping, set operations, generic relation types,
  negated relation filters, `latest()`/`earliest()` and `in_bulk()` retain their query semantics.
- `RETURNING` reads keyless models and converted keys correctly; old values retain their aliases.
  Bulk conflict reporting accounts for prepared defaults, tenant values and encrypted lookup keys.
- Generated fields retain their output field's conversion and comparison rules. Window defaults,
  duration expressions and literals in write expressions retain their declared field semantics.
  Functions unavailable on the selected dialect and JSON aggregates of unsupported nested field types
  are refused before SQL.
- Tenant scope caches follow live model changes and stay isolated between application contexts.
- Pydantic extra fields and related values respect `include` and `exclude`, including nested schemas.
  Request query declarations accept structurally compatible Meta options without requiring exact types.
- Migration serialization preserves import aliases in copied functions, decorators, defaults and
  annotations, including lambda scopes. Schema checks preserve distinct Unicode names on SQLite;
  tenant migrations retain one-shot target iterables.
- Rollback restores write-generated instance state; a cancelled `BEGIN` cannot leave a reusable
  connection in an unfinished transaction. Removing an outbox wakeup's last subscription closes its
  listener on the loop that opened it.
- Connection setup translates configuration failures; schema drift checks preserve non-PK identity
  targets, and generated typing stubs preserve shadowed imports and nested literal types.

## [0.11.0] - 2026-10-27

### Breaking changes

- `bulk_create(ignore_conflicts=True)` returns the objects it inserted, and only they are marked saved;
  objects whose keys the database generates need `on_conflict=[...]`.
- `JSONField(field_type=...)`, YAML configuration files and `Faker()` need the `pydantic`, `yaml` and
  `faker` extras.
- A transaction of `postgresql+asyncpg` refuses a statement from another event loop, and an in-memory
  SQLite database refuses another event loop - both with `ConfigurationError`.
- `bulk_update()` of a sliced queryset, or one of `distinct(*fields)`, raises `QueryError`, as `update()`
  does; it wrote objects outside the slice.
- A constraint or index condition, a policy or a `GeneratedField` reading a related model is refused when
  the model is bound (`ConfigurationError`), not by its migration.
- A `FloatField` filter by `NaN` on SQLite raises `ValidationError` - SQLite bound it as `NULL`.

### Added

- Outbox: `ChangeCapture(extend_many=...)` and `ChangeCapture.replace()`, `OutboxEvent.enqueue_many()`,
  topic patterns and `exclude_topics` of `OutboxRelay`, `WebhookDelivery` of subscribers (`target`,
  `allows_url`, `result_field`, `signature_header`).
- A `Q` as a value in `annotate()`, `alias()`, `values()`, `values_list()` and `update()` (`ConditionValue`).
- The pytest plugin's `hare_app_db` and `hare_app_transactional_db` for the application under test.
- `VersionedModel.get_last_visible_version_or_exception()`, `Uuid7`, `ClickhouseRequestQuery`,
  `Field.get_json_value()`, `prefetch_related(None)`.

### Changed

- The ids of `VersionedModel` and `OutboxEvent` are version 7 UUIDs; `ListenNotifyWakeup` sends one `NOTIFY`
  per topic and transaction; a PostgreSQL test database is dropped with its sessions.

### Fixed

- Migrations of outbox models, of a parameterized `field_type` and of an expression index over a foreign
  key; `makemigrations` without `asyncpg`; the change events of every field type; the pure wheel; the
  validators of a `UUIDField` given text; the JSON of a pydantic schema with NaN or infinity; the count,
  `exists()` and offset page of a request query that prefetches, `Exists()` of a queryset that prefetches.
- ClickHouse: the first `atomic()` of a process on a connection with `transactions=true`, and
  `autonomous()` with an option, which ran without a transaction; the Keeper migration lock of a database
  named with `/`; the key series of a database named as the prefix of another one, and a test database's
  series left in the Keeper; a `bloom_filter` index written without its argument reported as drift.
- Change capture: a write inside an open transaction whose event failed kept its row; `InProcessWakeup`
  signalled from a thread with an event loop of its own.
- `bulk_create(ignore_conflicts=True)` matching rows by a mapping's key order (hstore, jsonb).
- `inspectdb` and drift: SQLite `DATETIME`/`DOUBLE`/`FLOAT` columns and index order; a PostgreSQL `IN`
  check over `varchar` and a sequence shared by tables.
- A configuration, or a section of it, that isn't a mapping raises `ConfigurationError`, not `TypeError`;
  the `transactions` option of the application integrations is checked.

## [0.10.0] - 2026-10-26

### Breaking changes

- `DecimalField` is refused on SQLite.
- `Avg`, `StdDev` and `Variance` return what the database computes (`Decimal` on PostgreSQL).
- Settings written in code are no longer checked.
- `MEMORY_SQLITE`/`init_memory_sqlite()` are `MEMORY_DATABASE`/`init_memory_database()`.
- `Order` moved to `hare.sql.order`; the dialect API moved one database's SQL and features into its dialect.
- `get_model()` takes one `"app.Model"` label instead of `(app_label, model_name)`.

### Added

- `Meta.api_getter` of request queries, rollback of migrations, `<Model>Updates` and more types for
  mypy and `hare stubs`, `CHECK` of enum fields, narrowing checks of `AlterField`.
- Every API taking a model takes its `"app.Model"` label too.

### Fixed

- Bugs of writes, queries, migrations, inspectdb and ClickHouse found by an audit and a bug hunt.

## [0.9.0] - 2026-10-23

The first public release of hare-orm - an async Python ORM for PostgreSQL, SQLite and ClickHouse. It
brings the whole code base over from the project's earlier private development: hare-orm began as a
fork of [Tortoise ORM](https://github.com/tortoise/tortoise-orm) and carries an adapted copy of
[PyPika](https://github.com/kayak/pypika)'s query builder (see [NOTICE](NOTICE.md)), and most of both
has since been rewritten. Every entry below is new in this release.

It runs on CPython 3.12, 3.13 and 3.14, with PostgreSQL 14 and newer, SQLite 3.35.5 and newer and
ClickHouse 25.8 and newer - see [Installation](https://hare-orm.github.io/hare-orm/getting-started/installation/).
Wheels with the Rust extension are built for Linux (glibc and musl), macOS and Windows.

### Added

- **Models** - `Model` classes with typed fields, relations and `Meta` options: foreign keys,
  one-to-one and many-to-many relations (through models included), generic relations made of real
  foreign keys, composite primary keys everywhere - as foreign-key targets too - and models without
  a primary key; validators, `db_default` and generated columns, encrypted fields with blind indexes
  and sensitive fields hidden from errors and logs; models built at runtime from a table - see
  [Field types](https://hare-orm.github.io/hare-orm/models/field-types/),
  [Relations](https://hare-orm.github.io/hare-orm/models/relations/) and
  [Meta options](https://hare-orm.github.io/hare-orm/models/meta-options/).
- **Queries** - a lazy `QuerySet` with Django-style lookups across relations, `Q`/`F` expressions,
  annotations, aggregation (grouping sets included), window functions, `Case`/`When`,
  `Subquery`/`Exists`/`OuterReference`, set operations, CTEs and recursive queries over a tree,
  keyset pagination, `values()`/`values_list()`, `select_related()`/`prefetch_related()` with
  filters, bulk create and update, upserts, `MERGE`, `select_for_update()`, raw SQL through
  `RawSQLTerm` and the query builder - and an error instead of an aggregate a to-many JOIN would
  silently inflate - see [QuerySet methods](https://hare-orm.github.io/hare-orm/querying/queryset-methods/),
  [Filters](https://hare-orm.github.io/hare-orm/querying/filters/) and
  [Expressions](https://hare-orm.github.io/hare-orm/querying/expressions/).
- **Statement plans** - a query of a shape seen before runs on the SQL built for the first one,
  binding only its own values; repeated `get()`, filters, writes and instance saves skip building
  SQL altogether - see [Query plan cache](https://hare-orm.github.io/hare-orm/querying/query-plan-cache/).
- **The schema in `Meta`** - constraints (`EXCLUDE` included), partial and expression indexes,
  triggers, views and materialized views, database functions, sequences, row level security
  policies, grants, table partitioning and per-database table options, created and changed by
  migrations - see [Constraints and triggers](https://hare-orm.github.io/hare-orm/models/constraints-and-triggers/),
  [Indexes](https://hare-orm.github.io/hare-orm/models/indexes/) and
  [Schema objects](https://hare-orm.github.io/hare-orm/models/schema-objects/).
- **Behavior declared in `Meta`** - soft delete with restore, versioned rows, multi-tenancy by column,
  by schema or by row level security, optimistic locking, dirty-field tracking and change capture
  into a transactional outbox - see [Soft delete](https://hare-orm.github.io/hare-orm/soft-delete-versions-tenants/soft-delete/),
  [Versioned models](https://hare-orm.github.io/hare-orm/soft-delete-versions-tenants/versioned-models/) and
  [Multi-tenancy](https://hare-orm.github.io/hare-orm/soft-delete-versions-tenants/multi-tenancy/).
- **Migrations** - migrations detected from the changes to the models, written as files or run
  without them, squashed, checked for operations that lock a busy table (zero-downtime rules),
  compared with the live database (`hare drift`) and generated from an existing one
  (`hare inspectdb`); the `hare` CLI with its plugin commands - see
  [Migrations](https://hare-orm.github.io/hare-orm/migrations/migrations/),
  [Operations](https://hare-orm.github.io/hare-orm/migrations/operations/) and
  [CLI](https://hare-orm.github.io/hare-orm/migrations/cli/).
- **Connections and transactions** - pools with health checks, nested transactions with savepoints,
  isolation levels, read-only transactions and statement timeouts, `on_commit()`/`on_rollback()`
  callbacks, read replicas and routers with read-your-writes, several databases, distributed
  transactions by two-phase commit, PgBouncer in transaction mode - see
  [Transactions](https://hare-orm.github.io/hare-orm/connections/transactions/) and
  [Multiple databases](https://hare-orm.github.io/hare-orm/connections/multiple-databases/).
- **Pluggable dialects** - SQLite, PostgreSQL and ClickHouse are dialects built on the public
  `hare.dialects` API a third-party package uses; a database lacking a feature raises a clear error
  before any SQL is sent instead of returning a wrong result - see
  [Dialects and features](https://hare-orm.github.io/hare-orm/dialects/dialects-and-features/) and
  [Writing a dialect](https://hare-orm.github.io/hare-orm/extending/writing-a-dialect/).
- **PostgreSQL** - a Rust driver (`rust_pg`), the default for `postgresql://` URLs, decoding rows and
  converting values in Rust, and the `asyncpg` driver; arrays, ranges, hstore, ltree, network and
  native enum types, the GIN/GiST/BRIN/Bloom/Hash/SP-GiST index family, trigram search and
  `LISTEN`/`NOTIFY` - see [PostgreSQL fields](https://hare-orm.github.io/hare-orm/dialects/postgresql/fields/) and
  [LISTEN/NOTIFY](https://hare-orm.github.io/hare-orm/dialects/postgresql/listen-notify/).
- **SQLite** - through `aiosqlite`, with exact decimals, regular expressions and the date, text and
  math functions SQLite lacks registered on the connection, attached databases standing for
  schemas - see [SQLite: what runs differently](https://hare-orm.github.io/hare-orm/dialects/sqlite/differences/).
- **ClickHouse** - over HTTP (`clickhouse-connect`) and the native protocol (`clickhouse-driver`):
  MergeTree engines and their table options, clusters with `Distributed` tables, dictionaries,
  projections, `ARRAY JOIN`, `ASOF JOIN`, `LIMIT BY` and ClickHouse's own aggregates and types - see
  [ClickHouse models](https://hare-orm.github.io/hare-orm/dialects/clickhouse/models/) and
  [ClickHouse: what runs differently](https://hare-orm.github.io/hare-orm/dialects/clickhouse/differences/).
- **Search and geodata** - full-text search (PostgreSQL, SQLite FTS5), vector search (pgvector,
  sqlite-vec) and GIS (PostGIS, SpatiaLite, ClickHouse geometry types) - see
  [Full-text search](https://hare-orm.github.io/hare-orm/dialects/search-and-geodata/full-text-search/),
  [Vector search](https://hare-orm.github.io/hare-orm/dialects/search-and-geodata/vector-search/) and
  [GIS](https://hare-orm.github.io/hare-orm/dialects/search-and-geodata/gis/).
- **Integrations** - request queries turning HTTP parameters into a validated, paginated query;
  Litestar, FastAPI and Robyn integrations; Pydantic schemas of models; a transactional outbox with
  Kafka, RabbitMQ, Redis Streams, webhook and taskiq deliveries - see
  [Request queries](https://hare-orm.github.io/hare-orm/integrations/request-queries/),
  [Web frameworks](https://hare-orm.github.io/hare-orm/integrations/web-frameworks/) and
  [Outbox](https://hare-orm.github.io/hare-orm/integrations/outbox/).
- **Observability** - observers of queries and writes, OpenTelemetry spans and metrics, SQL comment
  tags, a repeated-query (N+1) detector and pool health - see
  [Observers](https://hare-orm.github.io/hare-orm/observability/observers/) and
  [OpenTelemetry](https://hare-orm.github.io/hare-orm/observability/opentelemetry/).
- **Testing and type checking** - a pytest plugin with isolated test databases, factories and
  query-count assertions; a mypy plugin rejecting misspelled filters and typing the rows of
  `values()`, and stubs for pyright - see [Test setup](https://hare-orm.github.io/hare-orm/testing/setup/) and
  [Type checking](https://hare-orm.github.io/hare-orm/querying/type-checking/).
- **Documentation** in English and Russian, and benchmarks against SQLAlchemy, Tortoise ORM,
  yara-orm and Django - see [Benchmarks](https://hare-orm.github.io/hare-orm/benchmarks/).

[Unreleased]: https://github.com/hare-orm/hare-orm/compare/v0.11.0...dev
[0.11.0]: https://github.com/hare-orm/hare-orm/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/hare-orm/hare-orm/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/hare-orm/hare-orm/releases/tag/v0.9.0
