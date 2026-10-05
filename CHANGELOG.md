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

The first public release of hare-orm, to be published as 0.9.0. It brings the whole code base over
from the project's earlier private development: hare-orm began as a fork of
[Tortoise ORM](https://github.com/tortoise/tortoise-orm) and carries an adapted copy of
[PyPika](https://github.com/kayak/pypika)'s query builder (see [NOTICE](NOTICE.md)), and most of
both has since been rewritten. Every entry below is new in this release.

### Added

- **Models and queries** - `Model` classes with `Meta` options, a lazy `QuerySet` with Django-style
  lookups, `Q`/`F` expressions, annotations, aggregation, window functions, `Case`/`When`,
  `Subquery`/`Exists`/`OuterRef`, set operations (`union()`/`intersection()`/`difference()`), CTEs
  (`with_cte()`), keyset pagination (`after_cursor()`/`before_cursor()`), `select_related()`/
  `prefetch_related()`, bulk create/update, upserts and `select_for_update()` - see the
  [QuerySet API](https://hare-orm.github.io/hare-orm/reference/queryset-api/) and
  [expressions and functions](https://hare-orm.github.io/hare-orm/reference/expressions-and-functions/).
- **Pluggable dialects** - SQLite and PostgreSQL are dialects built on the public `hare.dialects`
  API a third-party package uses: SQL, column types, DDL, introspection and server features come
  from the dialect; a database without transactions, foreign keys or unique constraints gets
  explicit errors - see [dialects](https://hare-orm.github.io/hare-orm/reference/dialects/).
- **A Rust PostgreSQL driver** (`rust_pg`), the default for `postgresql://` URLs, decoding rows and
  converting values in Rust; the `asyncpg` driver (`postgresql+asyncpg://`) is available instead.
- **Statement plans** - a query of a structure seen before runs on the SQL text built for the first
  one, binding only its own values: filters of any lookup and across relations, annotations and
  expressions of every kind, subqueries (correlated ones included), CTEs, set operations, keyset
  pagination, `values()`/`values_list()`, `count()`/`exists()`/`aggregate()`, `update()`/`delete()`
  and PostgreSQL's own expressions. Each part of a query describes its own plan
  (`Plannable.get_plan_description()`) - see [caching](https://hare-orm.github.io/hare-orm/reference/caching/).
- **Composite primary keys** on an equal footing with single-column ones - as foreign-key targets,
  with table-level `FOREIGN KEY` constraints, cascades, joins and `prefetch_related()`; models
  without a primary key.
- **Native `Meta` options** for optimistic locking, soft delete, versioning, multi-tenancy and
  dirty-field tracking - see [versioning and soft delete](https://hare-orm.github.io/hare-orm/reference/versioning-and-soft-delete/)
  and [multi-tenancy](https://hare-orm.github.io/hare-orm/reference/multi-tenancy/).
- **Declarative constraints and triggers** (`Meta.constraints`, `Meta.triggers`, PostgreSQL
  `EXCLUDE` and `CONSTRAINT TRIGGER`) applied when a migration runs.
- **A Django-style migration system and CLI** - `hare makemigrations`/`migrate`/`inspectdb`/...,
  migrations detected from changes to your models - see [migrations and CLI](https://hare-orm.github.io/hare-orm/reference/migrations-and-cli/).
- **PostgreSQL types, lookups and indexes** in `hare.dialects.postgresql` - `ArrayField`, range
  fields, `HStoreField`, `PostGISField`, `TSVectorField` and full-text search, `VectorField`
  (pgvector), trigram lookups, the GIN/GiST/BRIN/Bloom/Hash/SP-GiST/IVFFlat/HNSW index family - see
  [PostgreSQL extras](https://hare-orm.github.io/hare-orm/reference/postgres-extras/).
- **Transactions and several databases** - nested transactions, routers, distributed transactions -
  see [transactions and multiple databases](https://hare-orm.github.io/hare-orm/reference/transactions-and-multi-db/).
- **Request queries and web frameworks** - `hare.contrib.request_query` turns HTTP request
  parameters into a validated, paginated query; Litestar, FastAPI and Robyn integrations; pydantic
  model generation; a transactional outbox.
- **Observability** - query hooks, slow-query logging, a repeated-query (N+1) detector and
  OpenTelemetry spans - see [instrumentation](https://hare-orm.github.io/hare-orm/reference/instrumentation/).
- **Testing helpers** (`hare.contrib.test`) and documentation in English and Russian.

[Unreleased]: https://github.com/hare-orm/hare-orm/commits/dev
