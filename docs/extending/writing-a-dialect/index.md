# Writing a dialect

hare talks to a database through two objects. A **dialect** is the database's language: its SQL syntax,
column types, DDL and catalog. A **driver** connects to it: database URL schemes, credentials and the
client class that runs statements. One dialect can have several drivers — PostgreSQL has `asyncpg` and
`rust_pg`.

A new database is added from a package of its own; nothing in hare changes.

This guide follows Colstore, a made-up ClickHouse-like database, through every part (hare's own
ClickHouse dialect, `hare/dialects/clickhouse/`, is built the same way) — a columnar database without
transactions, foreign keys or unique constraints, with named placeholders, backtick quoting, tables that
need an engine and a sort key, and query modifiers (`FINAL`, `SAMPLE`) of its own.

## <a id="the-parts"></a>The parts

Everything a database decides lives in its dialect's package. hare's core never names a dialect, never
writes a database's own SQL and never checks a database's capabilities itself — it asks the dialect's
parts and reads `Features`. The `Dialect` object only assembles the parts: each one is built on first use
by a `build_*()` method the dialect overrides, and each one keeps its dialect as `dialect`.

| Part | Base class | `Dialect` member | What it decides |
|---|---|---|---|
| Features | `hare.dialects.base.features.Features` | `features` | Every "can the database do it" — one answer, read by the core as `connection.features` (or `dialect.features` without a connection). |
| Names and literals | `hare.dialects.base.literals.sql_literals.SqlLiterals` | `literals` / `build_literals()` | Quoting names, writing values into the SQL text. |
| Parameters | `hare.dialects.base.parameters.sql_parameters.SqlParameters` | `parameters` / `build_parameters()` | Placeholders, the casts a bare parameter needs, multi-row `INSERT` sources, COPY column types. |
| Expressions | `hare.dialects.base.renderers.term_renderers.TermRenderers` | `renderers` / `build_renderers()` | The SQL of functions and expressions the database writes differently. |
| Clauses | `hare.dialects.base.clauses.query_clauses.QueryClauses` | `clauses` / `build_clauses()` | `LIMIT`/`OFFSET`, row locks, `RETURNING`, `ON CONFLICT`, `DISTINCT ON`, `UPDATE ... FROM`, `EXPLAIN`. |
| Transactions | `hare.dialects.base.transactions.transaction_statements.TransactionStatements` | `transactions` / `build_transactions()` | The isolation level a transaction runs at and the statements that set it up after `BEGIN`. |
| Column types | `hare.dialects.base.types.TypeRegistry` | `types` / `build_types()` | How fields are stored and converted. |
| Lookups | `hare.dialects.base.lookups.filter_operators.FilterOperators` | `filter_operators` / `build_filter_operators()` | The operators lookups run with, and which lookups the dialect runs at all. |
| Full-text search | `hare.dialects.base.search.text_search.TextSearch` | `text_search` / `build_text_search()` | The SQL of [`hare.search`](../../dialects/search-and-geodata/full-text-search.md)'s queries, vectors, ranks and headlines. Optional: without it each raises `UnSupportedError`. |
| Schema editor | `hare.dialects.base.schema.base_schema_editor.BaseSchemaEditor` | `schema_editor_class` / `build_schema_editor_class()` | All DDL, for `generate_schemas()` and migrations alike. |
| Introspector | `hare.inspectdb.introspection.SchemaIntrospector` | `introspector_class` / `build_introspector_class()` | Reads an existing schema for `inspectdb` and `hare drift`. Optional. |
| Shadow database | `hare.dialects.base.schema.shadow.shadow_database.ShadowDatabase` | `shadow_database_class` / `build_shadow_database_class()` | Where `hare drift` builds the schema the models declare beside the database — see [the introspector](schema.md#introspector). Optional: without it drift raises `UnSupportedError`. |
| Table options | `hare.ddl.table_options.TableOptions` | `table_options_class` / `build_table_options_class()` | What the dialect's `CREATE TABLE` takes beyond columns. Optional. |
| Two-phase commit | `hare.dialects.base.transactions.two_phase_commit.TwoPhaseCommit` | `two_phase_commit` / `build_two_phase_commit()` | The statements of `Transactions.distributed()`. Optional. |
| Migration safety rules | `hare.dialects.base.migration_safety.MigrationSafetyRules` | `migration_safety_rules` / `build_migration_safety_rules()` | The rules `makemigrations` and `checkmigrations` apply, and how a table's rows are counted. |

Two more objects belong to a connection rather than to the dialect:

| Object | Base class | What it decides |
|---|---|---|
| Driver | `hare.dialects.base.connection.driver.Driver` | Name (the connection config's `engine`), database URL schemes and credentials, the client class, retryable errors. |
| Client | `hare.dialects.base.client.DatabaseClient` | One connection: running statements, transactions, the `Features` it has. |
| Query class | `hare.sql.builder.Query` | Binds statements to the dialect: a subclass that only sets `SQL_CONTEXT = dialect.sql_context`. |

Each base part writes ISO SQL; where ISO SQL has no form for something, its hook raises
`UnSupportedError` — the statement is refused before any SQL is sent. What several databases write alike
beyond ISO SQL is a class of `hare.dialects.base` too, named after what it holds:
`LimitReturningConflictQueryClauses` (`LIMIT`/`OFFSET`, `RETURNING`, `ON CONFLICT`, `UPDATE ... FROM`),
which the PostgreSQL and SQLite dialects extend. A third-party dialect of such a database starts from it,
any other from the base parts.

## <a id="registering"></a>Registering

The package's driver module registers the driver — and with it the dialect — when it is imported:

```python
# hare_colstore/driver.py
from hare.dialects.dialect_registry import DialectRegistry

COLSTORE_DRIVER = ColstoreDriver()
DialectRegistry.register_driver(COLSTORE_DRIVER)
```

and names that module in the `hare.dialects` entry point group, so hare imports it the first time it
looks up a driver that none of its own has, or lists every driver:

```toml
# pyproject.toml of hare-colstore
[project.entry-points."hare.dialects"]
colstore = "hare_colstore.driver"
```

Once the package is installed, a `"colstore+colstore-client://..."` connection URL or a connection config
with `"engine": "colstore+colstore-client"` uses it. A second driver or dialect under a taken name or
database URL scheme raises `ConfigurationError`. Registering a dialect calls its `install()` once — the
place to register what it adds to hare's own classes (a `QuerySet` method, a path transform on a core
field).

## <a id="checklist"></a>Checklist

- [ ] `Dialect` subclass: name, `otel_system_name`, `features`, and the parts the database writes
  differently — `build_literals()`, `build_parameters()`, `build_renderers()`, `build_clauses()`,
  `build_transactions()`, `build_types()`, `build_filter_operators()`.
- [ ] Query class with `SQL_CONTEXT = dialect.sql_context`.
- [ ] Client: statements, error translation, `features` from the dialect's plus the driver's;
  transactions where the database has them.
- [ ] Driver: name, schemes, credentials, client classes; registered on import.
- [ ] `hare.dialects` entry point.
- [ ] Schema editor templates and class methods, table options, introspector — as the database needs.
- [ ] hare's suite green under `HARE_TEST_DB`, including `tests/dialects/test_dialect_contract.py`.
