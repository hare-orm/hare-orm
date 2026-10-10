# Writing a dialect

hare talks to a database through two objects. A **dialect** is the database's language: its SQL
syntax, column types, DDL and catalog. A **driver** connects to it: DB_URL schemes, credentials and
the client class that runs statements. One dialect can have several drivers — PostgreSQL has
`asyncpg` and `rust_pg`.

A new database is added from a package of its own; nothing in hare changes. hare's test suite runs
against such a dialect too: `tests/dialects/columnar/` is a dialect built only from the public API
(numbered placeholders, backtick-quoted names, no transactions, foreign keys or unique constraints,
UUIDs stored as bytes, a `QuerySet` method and table options of its own), and the whole suite runs
green on it. Read it next to this guide.

This guide follows Colstore, a made-up ClickHouse-like database, through every part (hare's own
ClickHouse dialect, `hare/dialects/clickhouse/`, is built the same way) — a columnar database without
transactions, foreign keys or unique constraints, with named placeholders, backtick quoting,
tables that need an engine and a sort key, and query modifiers (`FINAL`, `SAMPLE`) of its own.

## <a id="the-parts"></a>The parts

Everything a database decides lives in its dialect's package. hare's core never names a dialect,
never writes a database's own SQL and never checks a database's capabilities itself — it asks the
dialect's parts and reads `Features`. The `Dialect` object only assembles the parts: each one is
built on first use by a `build_*()` method the dialect overrides, and each one keeps its dialect as
`dialect`.

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
| Full-text search | `hare.dialects.base.search.text_search.TextSearch` | `text_search` / `build_text_search()` | The SQL of [`hare.search`](../dialects/search-and-geodata/full-text-search.md)'s queries, vectors, ranks and headlines. Optional: without it each raises `UnSupportedError`. |
| Schema editor | `hare.dialects.base.schema.base_schema_editor.BaseSchemaEditor` | `schema_editor_class` / `build_schema_editor_class()` | All DDL, for `generate_schemas()` and migrations alike. |
| Introspector | `hare.inspectdb.introspection.SchemaIntrospector` | `introspector_class` / `build_introspector_class()` | Reads an existing schema for `inspectdb` and `hare drift`. Optional. |
| Table options | `hare.ddl.table_options.TableOptions` | `table_options_class` / `build_table_options_class()` | What the dialect's `CREATE TABLE` takes beyond columns. Optional. |
| Two-phase commit | `hare.dialects.base.transactions.two_phase_commit.TwoPhaseCommit` | `two_phase_commit` / `build_two_phase_commit()` | The statements of `Transactions.distributed()`. Optional. |
| Migration safety rules | `hare.dialects.base.migration_safety.MigrationSafetyRules` | `migration_safety_rules` / `build_migration_safety_rules()` | The rules `makemigrations` and `checkmigrations` apply, and how a table's rows are counted. |

Two more objects belong to a connection rather than to the dialect:

| Object | Base class | What it decides |
|---|---|---|
| Driver | `hare.dialects.base.connection.driver.Driver` | Name (the connection config's `engine`), DB_URL schemes and credentials, the client class, retryable errors. |
| Client | `hare.dialects.base.client.DatabaseClient` | One connection: running statements, transactions, the `Features` it has. |
| Query class | `hare.sql.builder.Query` | Binds statements to the dialect: a subclass that only sets `SQL_CONTEXT = dialect.sql_context`. |

Each base part writes ISO SQL; where ISO SQL has no form for something, its hook raises
`UnSupportedError` — the statement is refused before any SQL is sent. What several
databases write alike beyond ISO SQL is a class of `hare.dialects.base` too, named after what it
holds: `LimitReturningConflictQueryClauses` (`LIMIT`/`OFFSET`, `RETURNING`, `ON CONFLICT`,
`UPDATE ... FROM`), which the PostgreSQL and SQLite dialects extend. A third-party dialect of such
a database starts from it, any other from the base parts.

## <a id="registering"></a>Registering

The package's driver module registers the driver — and with it the dialect — when it is imported:

```python
# hare_colstore/driver.py
from hare.dialects.dialect_registry import DialectRegistry

COLSTORE_DRIVER = ColstoreDriver()
DialectRegistry.register_driver(COLSTORE_DRIVER)
```

and names that module in the `hare.dialects` entry point group, so hare imports it the first time
it looks up a driver that none of its own has, or lists every driver:

```toml
# pyproject.toml of hare-colstore
[project.entry-points."hare.dialects"]
colstore = "hare_colstore.driver"
```

Once the package is installed, a `"colstore+colstore-client://..."` connection URL or a connection config with
`"engine": "colstore+colstore-client"` uses it. A second driver or dialect under a taken name or DB_URL scheme
raises `ConfigurationError`. Registering a dialect calls its `install()` once — the place to register
what it adds to hare's own classes (a `QuerySet` method, a path transform on a core field).

## <a id="driver"></a>1. Connecting — the driver

```python
class ColstoreDriver(Driver):
    name = "colstore+colstore-client"    # the connection config's "engine"
    dialect = COLSTORE_DIALECT
    url_schemes = ("colstore+colstore-client",)  # colstore+colstore-client://user:password@host:9000/database
    path_credential = "database"              # what the DB_URL path fills
    authority_credentials = {"hostname": "host", "port": "port", "username": "user", "password": "password"}
    default_credentials = {"port": 9000}
    connection_options = ConnectionOptions(   # hare.dialects.base.connection.connection_options
        ConnectionOption("compression", ConnectionOptionType.BOOLEAN),
        ConnectionOption("connect_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=3600),
        ConnectionOption("max_block_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=1_000_000),
    )
    strict_query_parameters = True            # an unknown ?parameter is a ConfigurationError

    def get_client_class(self, credentials):
        return ColstoreClient

    def get_client_classes(self):
        return (ColstoreClient,)            # every client class, transaction clients included

    def is_retryable(self, error):
        return False                          # no transactions - nothing to retry as a whole
```

| Member | Meaning |
|---|---|
| `url_schemes`, `path_credential`, `authority_credentials`, `default_credentials`, `url_has_userinfo` | How a DB_URL becomes the client's keyword arguments. |
| `connection_options`, `strict_query_parameters` | The settings a connection takes, each declared once as a `ConnectionOption(name, value_type, *, minimum, maximum, positive, power_of_two, choices)` — its value type is `ConnectionOptionType.WHOLE_NUMBER`, `SECONDS`, `BOOLEAN`, `CHOICE` or `TEXT`. A setting is checked the same way whether it comes typed from a config dict's `credentials` or as text from a DB_URL query parameter (`?connect_timeout=5`): the client calls `options.read(settings)` in its `__init__` to take the known settings out, type- and range-checked, and `options.raise_for_unknown(settings, driver_name)` to reject a misspelled one. No hand-written parsing. |
| `get_client_class(credentials)` | The client class for a connection — it may pick one by a credential and remove it (SQLite's `install_regexp_functions`). |
| `get_client_classes()` | Every client class, transaction clients included. |
| `is_retryable(error)` | Whether a driver error means the database aborted the transaction for a concurrent one (serialization failure, deadlock); hare raises `TransactionRetryError` for it. |
| `get_url_path(url)`, `get_testing_path(path, reuse_databases)` | Reading the DB_URL path, and the path a test run connects to (the `{}` placeholder of `HARE_TEST_DB` filled with a fresh name). |

## <a id="client"></a>2. Running statements — the client

```python
class ColstoreClient(DatabaseClient):
    driver_name = "colstore+colstore-client"
    dialect = COLSTORE_DIALECT
    query_class = ColstoreQuery
    native_python_types = frozenset({str, int, float, bytes, datetime.datetime, datetime.date, uuid.UUID})
    # What the database supports is the dialect's; the client adds what its driver does.
    features = COLSTORE_DIALECT.features.replace(max_bind_parameters=100_000)
```

A client implements `create_connection(with_db)`, `close()`, `db_create()`, `db_delete()`,
`acquire_connection()` (a context manager handing out the raw connection) and the statement
methods — the whole contract between hare and a driver:

| Method | Runs |
|---|---|
| `execute(query, values=None, *, returns_rows=None, rows_by_position=False) -> StatementResult` | One statement. `StatementResult(row_count, rows, inserted_id=None, description=None)` (`hare.dialects.base.results`): the rows it returned — each read by column name — and the rows it changed for a write without `RETURNING`. `returns_rows` says whether the statement returns rows (a `SELECT`, a write with `RETURNING`); `None` leaves the driver to find out from the SQL text. `rows_by_position=True` is passed by a reader taking the rows by position alone where `Features.supports_positional_rows`: the driver may then give its cheapest rows read by position — plain tuples — with the DB-API `description` of the columns, which `StatementResult.column_names` reads (from the first row's names when no description is given). hare passes it for its own reads and for `UPDATE`/`DELETE` statements counting their rows. Every read and write of the ORM goes through it — there is no separate insert or dict-returning method to implement (`execute_dicts()` is built on it; set `row_to_dict` when the driver's rows convert faster than `dict(row)`). |
| `execute_many(query, values)` | One statement once per parameter row; returns nothing. |
| `execute_script(query)` | A script of several statements, passed on verbatim. |
| `execute_described(query, values=None) -> DescribedResult` | One statement with the names of its columns (`columns`, `rows` as tuples, `row_count`) — for showing any result as a table. |
| `copy(table, columns, records, column_types)` | A bulk load, where the database has such a protocol (`Features.supports_copy`). It is reported to the observers under the dialect's statement of it — `QueryClauses.get_bulk_load_statement_sql()`. |
| `_driver_stream_batches(query, values, chunk_size)` | An async generator of batches of rows (lists) over a server-side cursor, where `Features.supports_streaming` — on the transaction client; `chunk_size` 0 is the driver's own batch size. The public `stream_batches()` and `stream()` are the base class's: they tag, wrap and report the query around it. |

A statement's `values` are a `list`, or its subclass `QueryParameters`
(`hare.sql.terms.parameters.query_parameters`) when some of them hold a `sensitive=True` field's value:
`repr()` of it shows those as `<hidden>`, so a client logging its parameters with `%s` hides them
without doing anything. A driver that accepts no list subclass is given `list(values)`.

`native_python_types` is the set of Python types the driver hands back as a field wants them — the
row readers skip conversion for a field of such a type.

Each statement method turns the driver's own exceptions into hare's — `DBConnectionError`,
`IntegrityError`, and `OperationalError` or `TransactionRetryError` through
`_get_operational_error()`, which asks the driver's `is_retryable()` — and reports the call to
[observers and query wrappers](../observability/observers.md). Both happen in one decorator,
`DatabaseClient.translate_exceptions`, which the dialect's client class applies to the statement
methods of its drivers (`@ColstoreClient.translate_exceptions`). It appends the active query tags
(`DatabaseClient.get_tagged_query_arguments()`), runs the call inside the query wrappers when any
is installed (`Observers.run_wrapped(QueryCall(...), proceed)`), reports it to the observers once it
has finished, successful or not (`Observers.record_query()`), and raises what the driver raised as
the client's `translate_driver_error(client, error, sql, parameters, args, is_query_executing)`
returns — the one method a dialect's client overrides to map its driver's exceptions. The hooks the
decorator reads are class attributes of the client, read once when a method is wrapped:
`rejected_sql_character` (a character refused before the statement is sent),
`checks_aborted_transactions` and `aborted_transaction_message` (a statement refused in a
transaction the database has aborted), `runs_statement_options` (`command_timeout`,
`password_provider`, a transaction pooler, read retries). The decorator runs on every statement, so
it reads the client's `is_transaction_client` (true on every `TransactionClient`) rather than
calling `isinstance()`.

Once a connection opens, the client calls `_post_connect()`, which reads the server's version from
`get_server_version()` — `(major, minor, ...)`, read without query instrumentation; the default
`None` skips the check. A server older than `Dialect.minimum_server_version` is refused with
`UnSupportedError` and the connection closed; `Dialect.get_server_version_features(version)`
returns the `Features` values the version changes (PostgreSQL turns `supports_nulls_distinct` off
before 15 and leaves out the `EXPLAIN` options an older server lacks), applied to that connection
only. A transaction wrapper shares its connection's `features`.

A database with transactions implements `_get_transaction_client()` — a new client of the driver's
`TransactionClient` subclass, which gives the driver's primitives (see
[Transactions and concurrency](#transactions)). The transaction context around it is hare's own: it
takes the client's resources, begins, and commits or rolls back on exit. The statements run right
after `BEGIN` — isolation level, read only, statement and lock timeouts — come from the dialect's
`TransactionStatements`; a client that cancels an over-running statement itself, without a
statement of the dialect's, sets `enforces_statement_timeout_itself = True` (SQLite's interrupts the
query).

### <a id="several-drivers"></a>One dialect, several drivers

A dialect that has — or may get — more than one driver splits its client in two, the way hare's own
dialects do:

```text
hare/dialects/sqlite/
    client/
        sqlite_client.py               # SqliteClient: what any SQLite driver shares
        sqlite_transaction_client.py   # SqliteTransactionClient: the shared transaction lifecycle
        declarations.py                # SqliteDriverErrors: the driver's exception classes
    driver.py                          # SqliteDriver: the shared Driver base
    drivers/
        aiosqlite/
            driver.py                  # AiosqliteDriver: name, DB_URL schemes, registration
            client/
                aiosqlite_client.py    # AiosqliteClient(SqliteClient)
                aiosqlite_transaction_client.py
```

- **The shared client** holds what doesn't depend on the library that talks to the database: the
  connection settings and their checks, the transaction lifecycle (begin, commit, rollback,
  savepoints, aborted transactions), error translation, the pool status. It never imports a
  driver library. Where it needs a fact of the library — the exception classes, the library's own
  version and known faults — it reads a class attribute the driver's client sets: SQLite's clients
  set `driver_errors = SqliteDriverErrors(...)`, read only inside an `except` clause, so a
  statement that succeeds pays nothing for it.
- **A driver's client** subclasses it and implements what the library does: `create_connection()`,
  `acquire_connection()`, the statement methods, cursors and streaming, the transaction primitives
  (`_driver_commit()`, `_driver_savepoint()`, ...), `get_server_version()`, and the `Features` the
  library adds (`AiosqliteClient.features` adds the bind parameter limit of the `sqlite3` build).
  Its transaction client inherits the shared transaction client first:
  `AiosqliteTransactionClient(SqliteTransactionClient, AiosqliteClient)`.
- **The driver** keeps one name, its DB_URL scheme `<dialect>+<driver>`: `sqlite+aiosqlite://`,
  `postgresql+asyncpg://`, `clickhouse+clickhouse-connect://`, `clickhouse+clickhouse-driver://`. The
  dialect's plain scheme belongs to
  hare's own engine of the dialect — `postgresql://` (rust_pg); no other driver takes it.
- **The pool.** A client whose pool lives in Python counts in hare's `PoolStatistics`. A driver
  whose pool lives outside Python returns its own object with the same methods from
  `create_pool_statistics()`, and reports the occupancy from `get_pool_occupancy()` (with
  `Features.supports_pool_status`).
- **A native driver.** The parts of `rust.native` every native driver shares are outside any one
  database: `rust.native.pool` (`PoolStatistics`, `set_pool_metrics_enabled()`, the counted
  checkout of a deadpool pool) and the asyncio bridge its futures resolve through. A new native
  driver for SQLite or ClickHouse uses them as `rust.native.pg` does and is one more driver of the
  dialect — the shared client stays.

### <a id="password-provider"></a>Rotating passwords

A client whose database takes passwords that change supports `password_provider` the way
PostgreSQL's does ([Rotating credentials](../connections/connections.md#rotating-credentials)):

- Its settings include `PASSWORD_PROVIDER_OPTIONS` (`hare.dialects.base.connection.constants`) —
  `password_provider` and `password_refresh_seconds`, checked like any setting. A dialect without
  them refuses both as unknown settings.
- The client sets `self.password_provider = PasswordProvider.from_settings(settings, password)`
  (`hare.dialects.base.client.password_provider`) — None without a provider, `ConfigurationError`
  for a provider next to a fixed password.
- A driver that opens each connection itself asks `await self.password_provider.get()` there — the
  cached password, asked again once older than the refresh. A driver whose pool opens connections
  on its own overrides `apply_password(password)` to hand the pool a new password, starts
  `password_provider.start_refreshing(self.apply_password)` once the pool is open and stops it with
  `await password_provider.stop_refreshing()` when the pool closes.
- When the server refuses a password, `await client.renew_password()` asks the function right away
  and applies the answer; a client runs the refused statement once more only when it knows the
  statement never reached the server.

### <a id="shell-command"></a>The interactive client

`hare dbshell` runs what `await client.get_shell_command()` returns — a `ShellCommand`
(`hare.dialects.base.client.declarations`): the program with its arguments, and the variables
added to its environment. A password goes into the environment, never among the arguments other
users of the machine see. The default `None` makes `hare dbshell` refuse the dialect; a connection
no other program can open (an in-memory database) raises `UnSupportedError` with the reason.

### <a id="features"></a>Features

`Features` (`hare.dialects.base.features`) are the one place hare asks what a database and its
driver support. The dialect declares what the database supports (`Dialect.features`), the client
starts from them and sets what its driver adds, and the server version adjusts them per connection.
The core reads `connection.features` where it has a connection and `dialect.features` where it has
only the dialect (DDL rendered for a migration file, a lookup list).

| Feature | hare's reaction when it's off |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` raise `UnSupportedError`; hare's own multi-statement writes (a cascade, an M2M `add()`, batched `bulk_create()`) run their statements one by one. |
| `supports_savepoints` | A nested `atomic()` joins the transaction it is nested in; an error leaving the nested block makes the transaction roll back as it ends and raise `TransactionManagementError`, even if the outer block caught the error. |
| `supports_generated_keys` | A model whose primary key the database generates (`IntField(primary_key=True)` by default) is refused with `ConfigurationError` when bound to the connection — give it a key the application sets (`UUIDField(primary_key=True, default=uuid.uuid7)`). hare's own migration journal is keyed by `(app, name)`. |
| `takes_keys_before_insert` | Set it where the keys come from a series before the rows are written: hare calls the client's `take_generated_keys(model, count)` for the rows without a key and writes them with their keys; `synchronize_key_series(model)` moves the series past the table's greatest key (`SynchronizeKeySeries`). |
| `checks_constraints_before_write` | Set it where the database keeps neither unique constraints nor foreign keys: hare checks a model's declared uniqueness and relations with a `SELECT` per batch before `create()`, `save()`, `bulk_create()`, `update()` and `bulk_update()` and raises `IntegrityError`; a table whose options say `keeps_row_versions()` has its key left unchecked. An upsert without `ON CONFLICT` reads the conflicting keys first and inserts or updates by them. |
| `returns_rows_by_reading` | Set it where there is no `RETURNING`: `update().returning()` reads the keys before and the rows after the write, `delete().returning()` the rows before it, `bulk_create(returning=True)` the rows after the insert — all by their keys; `returning(old=...)` stays refused. |
| `supports_row_updates` | Every `UPDATE` — `save()` of a stored row, `QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` of a cascade — raises `UnSupportedError` before it is sent, and a cascade needing one writes nothing. A soft-deleted model (`Meta.soft_delete_field`) is refused with `ConfigurationError` when bound; the outbox relay refuses to start. |
| `supports_row_deletes` | Every `DELETE` raises `UnSupportedError` before it is sent, and a cascade needing one writes nothing. |
| `rewrites_correlated_exists` | Set it where the database runs correlated subqueries but computes a correlated `EXISTS` wrongly: such an `EXISTS` keeps its `IN` form. |
| `supports_ordered_correlated_subqueries` | A correlated subquery ordering or slicing its own rows raises `UnSupportedError` before the statement is sent. |
| `orders_by_correlated_subqueries` | A query with a correlated subquery in `ORDER BY`, or selected beside a `WHERE`, selects its rows in a derived table — the conditions and sort keys hidden columns of it — and filters, orders and slices them outside; an `UPDATE`/`DELETE` with a correlated subquery raises `UnSupportedError`. |
| `supports_correlated_subqueries` | An `EXISTS` correlated by equal columns — a filter or an exclusion across a to-many relation, `<m2m>__isnull`, `Exists(...)` with `OuterReference` equalities — is written as `(outer columns) IN (SELECT inner columns ...)` with NULLs ruled out, true and false exactly where the `EXISTS` is. Any other correlated subquery (`Subquery(...)` with `OuterReference`, an `OuterReference` compared other than by `=`) raises `UnSupportedError` before the statement is sent; so does the outbox relay's start. |
| `can_rollback_ddl` | A migration isn't wrapped in a transaction. |
| `supports_select_for_update` | `select_for_update()` raises `UnSupportedError` when the query runs; `update_or_create()` skips its lock. |
| `locks_rows_by_key` | Set it where rows are locked outside SQL: `select_for_update()` reads the keys of its rows in the transaction, hands their names (`<table>/<key>`, sorted) to the transaction client's `take_row_locks(lock_names, wait=...)`, which returns a `RowLockOutcome` (`taken`, `busy`, `waited`), and reads the rows by their keys; a row waited for is read again outside the transaction and a changed one raises `TransactionRetryError`. The statement carries no lock clause. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` takes a plain `FOR UPDATE` lock. |
| `supports_select_for_share` / `supports_select_for_key_share` | `select_for_update(share=True)` / `(key_share=True)` raises `UnSupportedError` when the query runs. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` of a sliced queryset pick their rows through a `pk IN (SELECT ...)` subquery. |
| `supports_returning` | An `INSERT` asks for nothing back: a key the database generates isn't read onto the instance (give such models a key the application sets), and the values of `db_default` columns are read with a `SELECT` by primary key. An `UPDATE` doesn't read changed generated columns back, and a bulk write can't tell which rows it really inserted. |
| `guarantees_returning_order` | A multi-row `INSERT ... RETURNING` is matched back to its objects by primary key instead of by position. |
| `supports_posix_regex` | A filter with `posix_regex`/`iposix_regex` raises `UnSupportedError` before the query runs. |
| `supports_two_phase_commit` | `Transactions.distributed()` rejects the connection. |
| `supports_listen_notify` | The transactional outbox sends no `NOTIFY` for a new event. |
| `supports_streaming` | `QuerySet.stream()` is unsupported. |
| `streams_without_transaction` | `stream()` raises `QueryError` outside a transaction; set it where the client's `stream_batches()` streams on a connection of its own. |
| `supports_copy` | `bulk_create(use_copy=True)` raises `UnSupportedError`. |
| `copies_bulk_inserts` | `bulk_create()` loads rows through `copy()` only with `use_copy=True`. Set it where `copy()` is the database's usual way to write many rows: `bulk_create()` then takes it whenever it handles no conflict and reads no row back. |
| `inline_comments` | Table and column comments go into `CREATE TABLE` instead of `COMMENT ON`. |
| `supports_positional_rows` | Rows are read by column name only — no `execute(rows_by_position=True)`, no native reading of rows and no run of a queryset on the plan of its calls. |
| `execute_many_scales_poorly` | Bulk writes are sent as multi-row statements instead of `executemany()`. |
| `binds_written_parameters` | Rows written by the native writer are bound as Python objects. |
| `binds_array_parameters` | A list bound as one parameter (a `RawSQL` parameter such as `= ANY(%s)`) raises `UnSupportedError`. |
| `max_bind_parameters` | Bulk writes, prefetching and cascades split their statements to stay under it. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` raises `UnSupportedError` before its DDL is sent. |
| `supports_unhex` | A long `__in` of bytes binds one parameter per value instead of one JSON array. |
| `supports_drop_column` | A schema editor that rebuilds tables (SQLite's) rebuilds one for every removed field. |
| `explain_options` | An `EXPLAIN` option not listed raises `UnSupportedError` before the statement is sent. |
| `supports_schemas` | A schema-qualified model's table is used unqualified. |
| `supports_distinct_on` | `distinct(*fields)` picks the first row of each combination by `ROW_NUMBER()` in a `pk IN` subquery. |
| `supports_grouping_sets` | `group_by(Rollup/Cube/GroupingSets(...))` and `Grouping()` raise `UnSupportedError`. |
| `supports_lateral` | `Lateral(...)` raises `UnSupportedError`. |
| `supports_table_sample` | `QuerySet.sample(...)` raises `UnSupportedError`. |
| `supports_asof_join` | `AsofJoin(...)` raises `UnSupportedError`. |
| `supports_array_join` | `ArrayJoin(...)` raises `UnSupportedError`. |
| `supports_lightweight_update`, `rebuilds_projections`, `supports_json_type`, `supports_variant_types`, `supports_refreshable_materialized_views` | ClickHouse's own, by the server's version (`get_server_version_features()`): a lightweight `UPDATE`, projections kept right by a lightweight `DELETE`, the `JSON` type, the `Variant` and `Dynamic` types, materialized views refreshed on a schedule. |
| `supports_dictionaries` | `Meta.dictionaries` raises `UnSupportedError` before its DDL; else the schema editor's `dictionaries_class` (`Dictionaries`: `get_dictionary_create_sqls()`, `drop_dictionary()`, `alter_dictionary()`, `rename_dictionary()`, `reload_dictionary()`). |
| `supports_merge` | `QuerySet.merge()` raises `UnSupportedError`. |
| `supports_merge_returning` | `merge().returning()` raises `UnSupportedError`. |
| `supports_merge_not_matched_by_source` | `merge().when_not_matched_by_source()` raises `UnSupportedError`. |
| `supports_partitioned_exclusion_constraints` | An `ExclusionConstraint` on a partitioned model raises `UnSupportedError`. |
| `supports_pool_status` | `get_pool_status()` raises `UnSupportedError`, and the client's pools are left out of `Connections.get_pool_statuses()`. |
| `sorts_nulls_first` | Where NULL sorts by default — `nulls_first`/`nulls_last` add the clause only where it changes the order. |
| `enforces_numeric_ranges` | hare checks integer and decimal ranges itself before writing. |
| `supports_conflict_constraint_names`, `supports_conflict_where` | `bulk_create(on_conflict_constraint=...)` / `conflict_where=` raise `UnSupportedError`. |
| `matches_ordering_to_grouping_by_sql` | Set it when an ordering term that is also grouped by has to be written exactly as in `GROUP BY`. |
| `supports_virtual_generated_columns` | `GeneratedField(stored=False)` raises `UnSupportedError`. |
| `supports_strict_tables` | `SqliteTableOptions(strict=True)` raises `UnSupportedError` — a SQLite table option. |
| `supports_text_search_configurations` | A text search configuration, a `SearchVector` value, lexemes, `SearchRank`'s label weights, `normalization` and `cover_density`, and `SearchHeadline`'s fragment options raise `UnSupportedError`. |
| `supports_full_text_index` | `FullTextIndex` and `SearchRank`'s weights by field raise `UnSupportedError`. |
| `supports_vector_search` | The vector distances and `__nearby` raise `UnSupportedError`. |
| `supports_tenant_schemas` | `TenantSchemas.create()`/`drop()`/`get_tenants()` raise `UnSupportedError`; implement the client's `get_tenant_client_settings(schema_name)` (the search path of a tenant's schema), the connection option `tenant_schema_template` and the introspector's `fetch_schema_names()`. |
| `supports_spatial` | The `hare.gis` lookups, paths, functions and aggregates raise `UnSupportedError`; register renderers of `GeometryValue`, `SpatialRelationTerm`, `SpatialFunctionTerm` and the names of `SpatialAggregateFunction`, and a `GeometryField` column type with its value conversions. |
| `supports_geography` | The lookups and functions of a `GeometryField(geography=True)` raise `UnSupportedError`. |
| `supports_spatial_index` | `SpatialiteIndex` raises `UnSupportedError` — it is SQLite's own; a dialect with a spatial index of its own declares its own index class. |
| `spatial_reference_ids` | None: a geography and a spatial index take any SRID. A set of SRIDs — those of the database's spatial metadata, read when the connection opens — and a geography or a spatial index in another SRID raises `UnSupportedError`. |
| `supports_ordered_aggregates` | An aggregate with `order_by=` that has no other form raises `UnSupportedError` (`MakeLine`). |
| `supports_uuid_v7` | A `UuidV7()` `db_default` raises `UnSupportedError` before the DDL. |
| `supports_without_overlaps` | `UniqueConstraint`/`CompositePrimaryKey` `without_overlaps=True` raises `UnSupportedError`. |
| `supports_returning_old_new` | `returning(old=...)` raises `UnSupportedError`; else `QueryClauses.get_old_row_value_sql()`. |
| `supports_json_table` | `JsonTable` raises `UnSupportedError`; else `QueryClauses.get_json_table_sql()` (ISO by default). |
| `isolation_levels` | The levels a transaction runs at, weakest first — see [Transactions](#transactions). |
| `max_identifier_length` | The most bytes a name may take, None for no limit. hare generates names of up to 63 bytes, shortening a longer one with a digest; a dialect with a lower limit can't be registered. |
| `cascade_depth_limit`, `checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` | How a native cascade behaves — see [without guarantees](#without-guarantees). |

The DDL features are listed with [the schema editor](#schema-editor). A client class can derive
another's features: `AiosqliteClient.features.replace(supports_posix_regex=True)`.

### <a id="executor"></a>Model-level statements

There is nothing per driver between a model and `execute()`: the same write pipeline builds every
`INSERT`, `UPDATE`, upsert and `DELETE` — `save()`, `bulk_create()`, `QuerySet.update()` alike —
and the same row readers build instances from what `execute()` returns. What differs between
databases is asked from the dialect's parts and the connection's `Features`: generated and
database-default columns come back through `INSERT ... RETURNING` where `supports_returning`;
`clauses.get_upsert_inserted_flag_sql()` gives the `RETURNING` expression that tells an inserted
row of an upsert from an updated one (PostgreSQL's `xmax = 0`; `None` makes hare read the existing
keys before the write, and only while an observer listens).

## <a id="dialect"></a>3. Names, literals and parameters — `SqlLiterals`, `SqlParameters`

```python
class ColstoreLiterals(SqlLiterals):
    identifier_quote_char = "`"
    alias_quote_char = "`"


class ColstoreParameters(SqlParameters):
    placeholder_template = "{{p{}}}"          # {p1}, {p2}, ...


class ColstoreDialect(Dialect):
    name = "colstore"
    otel_system_name = "colstore"           # OpenTelemetry db.system.name
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_foreign_keys=False,
        supports_unique_constraints=False,
    )

    def build_literals(self):
        return ColstoreLiterals(self)

    def build_parameters(self):
        return ColstoreParameters(self)
```

`SqlLiterals` writes names and values into the SQL text; names and literals are escaped only here —
the schema editor, the SQL renderer and the introspector all ask it:

| Member | Meaning |
|---|---|
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)`, `qualify_table_name(table, schema)` | Quoting of table/column/index/constraint names and of SELECT aliases. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_boolean_literal_sql(value)`, `get_bytes_literal_sql(value)`, `get_array_literal_sql(element_sqls)` | Literals written into SQL text — a string, a column default, a boolean of a query, bytes, an array. |

`SqlParameters` binds values:

| Member | Meaning |
|---|---|
| `placeholder_template`, `get_placeholder(index)`, `numbers_parameters` | A bound parameter's placeholder, `{}` standing for its index from 1 (`$1` on PostgreSQL, `?` on SQLite). Cached query shapes substitute values by position, so any placeholder style works with the query cache. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)` | The type a bound literal is cast to where nothing around the parameter types it — by where it stands (`ParameterPosition`: a `CASE` branch, a selected or compared literal, a function argument), as a column's value, as a value of a JSON object, or as rendered SQL. None or the SQL unchanged by default; PostgreSQL, which types a parameter only from what's around it, casts. |
| `get_bindable_number(value)` | How a number standing for a JSON number is bound. |
| `single_parameter_in_list_min_length` | The length from which an `__in`/`__not_in` list binds as one parameter. |
| `get_default_rows_source_sql(row_count)`, `get_column_arrays_rows_source_sql(column_types, first_index)` | The sources a multi-row `INSERT` reads rows of defaults, or of one array per column, from — None for a statement per row and `VALUES`. |
| `get_copy_column_type(field)` | The type a bulk load (`copy()`) is told a field's column has — the type the table declares by default. |
| `supports_copy_column_type(column_type)` | Whether the bulk load loads a column of that type. |

`Dialect.sql_context` is the context every statement renders in; the query class sets
`SQL_CONTEXT = dialect.sql_context`.

## <a id="types"></a>4. Column types and values — `build_types()`

`build_types()` returns a `TypeRegistry` (`hare.dialects.base.types`) mapping field classes to how
the dialect stores them. A field class uses the mapping of its nearest registered base class, so a
third-party field inherits its parent's storage:

```python
def build_types(self):
    types = TypeRegistry()
    types.register(BigIntField, TypeMapping(column_type="Int64"))
    types.register(DatetimeField, TypeMapping(column_type="DateTime64(6, 'UTC')"))
    types.register(UUIDField, TypeMapping(column_type="UUID"))
    types.register(CharField, TypeMapping(column_type=lambda field: "String"))
    types.register(
        DecimalField,
        TypeMapping(column_type=lambda field: f"Decimal({field.max_digits}, {field.decimal_places})"),
    )
    return types
```

| `TypeMapping` member | Meaning |
|---|---|
| `column_type` | The column type, or a `(field) -> str`. Without, the field's own `SQL_TYPE`. |
| `generated_sql` | The DDL of a database-generated primary key column. |
| `function_cast` | A `(field, term) -> term` wrapped around the column wherever it is compared, ordered or copied. |
| `to_db`, `to_lookup`, `to_python` | Replace the field's `to_db_value()`, `to_lookup_value()` and `from_db_value()` — each value binds and reads through the query's connection. |
| `json_term` | The text a column's value is written into a JSON object as, for a value stored in a form JSON can't hold (the columnar dialect's 16-byte UUIDs). |
| `extension` | The database extension the column type needs (`postgis` for PostgreSQL's spatial types) — created wherever a field of the class is used, and added to migrations as `CreateExtension`. |
| `naive_datetime_is_utc` | `to_python` is the field's own `from_db_value()`, with a naive datetime from the driver read as a UTC instant. |
| `inserted_by_select` | An INSERT writing the column writes its rows as a `SELECT` of them, not as `VALUES` — for a value written as an expression the dialect's `VALUES` doesn't read; or a `(field) -> bool` deciding it per field. |

`Features` describe how values behave: `enforces_numeric_ranges` (without, hare checks integer and
decimal ranges itself), `supports_virtual_generated_columns`. Where a value is stored or computed
differently, the dialect's `TermRenderers` give the SQL ([next section](#functions-and-lookups)). A
function the database has only for some argument types (PostgreSQL's `ROUND(numeric, int)`) is a
renderer too.

A package adding a field can register its storage on any dialect:

```python
DialectRegistry.get_dialect("colstore").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## <a id="functions-and-lookups"></a>5. Expressions and lookups — `TermRenderers`, `FilterOperators`

Every expression renders standard SQL by default. `build_renderers()` returns the `TermRenderers`
that replace what the database writes differently, found through the term's class hierarchy:

```python
def build_renderers(self):
    renderers = TermRenderers(self)
    renderers.register_function("LENGTH", self.render_length)         # (function, sql_context) -> sql
    renderers.register_name(functions.Coalesce, lambda term, sql_context: "ifNull")
    return renderers

@staticmethod
def render_length(length, sql_context):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], sql_context)})"
```

hare's own dialects build theirs on `CheckedTermRenderers` (`hare.dialects.base.renderers`): its
`add_own_renderers()` registers the dialect's renderers, grouped by theme into classes of one
`register(renderers)` each — `<Dialect>JsonRenderers`, `<Dialect>TemporalRenderers`,
`<Dialect>NumberRenderers`, `<Dialect>TextRenderers`, and those of spatial terms, vectors and
full-text search. Building it checks that every term each of hare's dialects must render — the JSON,
temporal, numeric and text terms the core leaves to the dialect — has a renderer, and raises
`TypeError` naming the missing ones. A third-party dialect may start from it the same way, or from a
plain `TermRenderers`.

A subclass of `TermRenderers` also gives the terms the dialect puts in place of an expression where
its SQL needs another:

| Member | Meaning |
|---|---|
| `is_distinct_from_operator` | The NULL-safe inequality (`IS NOT` on SQLite). |
| `get_decimal_compared_term()`, `get_decimal_value_term()`, `get_decimal_dividend()`, `get_assigned_decimal_term()` | Decimals stored as text: compared, mixed with other numbers, divided, assigned by an `UPDATE`. |
| `get_json_path_comparand()` | What the JSON value at a path is compared with — JSON stored as text. |
| `get_integer_aggregate_as_float()` | An average or a statistic of integers, read as a float. |
| `get_datetime_part_comparand()` | A date or time of day compared with a datetime's part. |
| `get_concatenated_argument_sql(sql, argument)` | An argument of a text concatenation, cast where the database types it only from what's around it. |
| `get_ordering_term()` | What `ORDER BY` sorts by for a term. |
| `get_composite_distinct_key()` | What `COUNT(DISTINCT ...)` counts a composite key by. |
| `get_never_null_column_count_argument(term)` | What `COUNT` takes for a column of the queried table that holds no NULL — the column by default; `*` where counting rows is cheaper than reading the column. |
| `get_connection_only_function(sql)` | A function hare installs on its own connections that DDL can't use. |

`build_filter_operators()` returns the `FilterOperators` — a mapping from a lookup's own operator to
the dialect's replacement (`FilterOperators(self, {Lookups.is_in: colstore_is_in, ...})`, `{}` for
none). An operator only dialects implement (`DialectImplementedOperators`) has to be replaced, or
the lookup is unsupported: `FilterOperators.supports_lookup()` — and with it
`Model._meta.get_lookups(path, dialect)` — leaves it out, and a filter using it fails before any SQL
is built.

A value list too long to bind one parameter per value is the dialect's to shorten: subclass
`LargeInList` (`hare.dialects.base.parameters.large_in_list`) and map `Lookups.is_in`/`not_in`/`row_is_in`/
`row_not_in` to its methods of the same names in `build_filter_operators()`. The base class holds
the algorithm once — when the short form applies, how `NULL`s in the list are compared apart, how
`not_in` is negated, the row-value form for a composite key; the dialect gives only the container
the values are bound into as **one** parameter: `get_membership_criterion(field, values,
non_null_values, element_type)` (PostgreSQL's `= ANY($1::type[])`, SQLite's `IN (SELECT value FROM
json_each(?))`) and `get_row_container(value_rows, element_types)` for value rows. Returning `None`
keeps the plain `IN (...)` list.

A renderer changes how a term's SQL is written, never what a query's plan is keyed by — the dialect
is part of every plan key. An expression class of the dialect's own (PostgreSQL's array, trigram and
spatial functions) declares how its attributes meet the plan in `plan_parts` and resolves its
arguments with `ExpressionArguments.get_result()`, as any
[custom expression](custom-functions-and-expressions.md#writing-a-custom-expression) does.

## <a id="query-class"></a>6. Statement clauses — `QueryClauses`

There is one `QueryBuilder` for every database. It renders ISO SQL and asks the dialect's
`QueryClauses` for every clause databases write differently; the base `QueryClauses` writes ISO SQL
(`OFFSET ... ROWS FETCH FIRST ... ROWS ONLY`) and raises `UnSupportedError` for a clause ISO SQL
lacks:

| Member | Writes |
|---|---|
| `get_statement_context(builder, sql_context)` | The context a statement's own terms render in — the same one by default; a dialect whose renderers need to know something of the statement around a term (that it is grouped) returns a context of its own holding it. |
| `get_bulk_load_statement_sql(table, columns)` | The statement a bulk load (`copy()`) is reported under to observers, spans and the query counter. |
| `get_distinct_sql(builder, sql_context)` | `DISTINCT` / `DISTINCT ON (...)`. |
| `get_row_lock_sql(builder, sql_context)` | A row lock (`select_for_update()`): `FOR UPDATE`, `NO KEY`, `OF`, `NOWAIT`, `SKIP LOCKED`. |
| `get_statement_end_sql(builder, sql_context)` | What a subquery, or a query with clauses of the dialect's `QuerySet` methods, ends with after its `LIMIT` — nothing by default; ClickHouse writes its `SETTINGS` there, giving a joining subquery `join_use_nulls = 1`, which a mutation's condition runs without otherwise. |
| `get_main_table_suffix_sql(builder, sql_context)` | What follows the first table of `FROM` — its sample (`get_table_sample_sql()`) by default; ClickHouse writes `FINAL` and `SAMPLE ... OFFSET ...` there. |
| `get_prewhere_sql(builder, sql_context)`, `get_limit_by_sql(builder, sql_context)` | A condition read before `WHERE`, after the JOINs; a limit of rows per group, after `ORDER BY` — nothing by default. |
| `get_limit_offset_sql(limit_sql, offset_sql)` | The bounds of a query's rows. |
| `get_returning_sql(returned)` | `RETURNING`, from `ReturnedValue`s — a value's SQL, the alias it is returned under, the name of a plain column. |
| `get_lateral_sql(subquery_sql)` | A subquery joined `LATERAL` — ISO `LATERAL (...)` by default. |
| `get_table_sample_sql(method, percent_sql, seed_sql)` | The sample of a table in `FROM` — ISO `TABLESAMPLE ... REPEATABLE (...)` by default. |
| `get_merge_sql(...)`, `get_merge_when_sql(when)`, `get_merge_returning_sql(returned, action_alias_sql)` | A `MERGE` from rendered parts (`MergeWhenSql`) — ISO `MERGE` by default; a `DO NOTHING` branch, `WHEN NOT MATCHED BY SOURCE` and `RETURNING` raise `UnSupportedError` there. |
| `get_on_conflict_sql(builder, sql_context)` | The conflict handling of an `INSERT`. |
| `get_update_sql(builder, sql_context)` | An `UPDATE` — reading other tables in `FROM`, ordering and limiting its rows where the database can. |
| `get_delete_sql(builder, sql_context)` | A `DELETE` — ISO `DELETE FROM` with its conditions by default; a database deleting rows another way (an `ALTER TABLE ... DELETE` mutation) writes its own. |
| `get_typed_placeholder_template()`, `get_values_table_columns_sql()`, `get_update_from_values_sql()`, `get_insert_rows_source_sql()` | The statements hare writes as text: an `UPDATE` from a `VALUES` table (`bulk_update()`), an `INSERT` of rows from a source. |
| `get_upsert_inserted_flag_sql()` | The `RETURNING` expression telling an upsert's inserted row from an updated one. |
| `get_explain_sql(sql, output_format, options, features)` | The `EXPLAIN` statement `QuerySet.sql(explain=True)` and `explain()` use. |

The query class only binds statements to the dialect:

```python
ColstoreQuery = DeclaredSubclass.make(
    Query, "ColstoreQuery", __package__, "A query in Colstore's SQL.", SQL_CONTEXT=COLSTORE_DIALECT.sql_context
)
```

### <a id="queryset-methods"></a>`QuerySet` methods of the dialect

A database's own query modifiers become `QuerySet` methods without hare knowing them. Register
them in `install()` with a function that gets the query's builder and the call's arguments and
returns the builder — the columnar test dialect's `random_share()`:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("random_share", self.name, self.apply_random_share)

def apply_random_share(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).random_share(10)` records the call — before `Hare.init()` too — and the query applies
it when it is built for its connection; on a connection of another dialect it raises
`UnSupportedError`. A package can register a method for a dialect it doesn't define.

`QuerySetExtensions.register(name, dialect_name, apply)` (from `hare.query.queryset.extensions`) is
usually called in the dialect's `install()`. `apply(builder, *args, **kwargs)` returns the query
builder with the call applied. A call is recorded on the queryset (before `Hare.init()` too) and
carried into `values()`, `count()` and the other queries built from it; when the query is built for
its connection, the implementation of that connection's dialect applies it. On a connection whose
dialect registered none, the query raises `UnSupportedError` instead of dropping the call. A name
QuerySet already has, or a private one, is refused with `ConfigurationError`. The name and arguments
of each call are part of the plan key — written into the query after it is built, they are part of
its SQL text; a call with an argument that can't be part of a key (a list, a dict) keeps the query
from keeping a plan. An argument that describes itself to a plan — a `Q`, an expression — is part of
the key by its structure, its values bound like a filter's.

`register()` takes four more keyword arguments, for a method that needs more than the builder:

| Argument | Meaning |
|---|---|
| `reads_query=True` | `apply(builder, extension_query, *args, **kwargs)` gets the query too — a `QuerySetExtensionQuery`: its `model`, `connection`, `dialect`, `table`, whether it `is_write` or `is_summary`, and `get_condition(q)`, `get_expression(name_or_expression)` and `join(builder, joins)`, which resolve a condition or a field as the query's own filters are, their values recorded into its plan. |
| `takes_condition=True` | The method takes `filter()`'s arguments — `Q` objects and filters — recorded as one `Q` that `apply` gets (ClickHouse's `prewhere()`). Every dialect registering the name declares it alike, or `register()` raises `ConfigurationError`. |
| `changes_rows=True` | A call changes which rows the query returns beyond its conditions (ClickHouse's `limit_by()`): `count()` and `exists()` count the rows of the query as a derived table. |
| `read_result=` | `await read_result(extension_query, result, *args, **kwargs)` reads the result of a rows query with the call and returns what it returns (ClickHouse's `with_totals()` runs a second statement and returns its rows with the totals). |

`QueryBuilder.set_dialect_clause(name, value)` keeps what a call sets for the dialect's
`QueryClauses` to write — in `get_main_table_suffix_sql()`, `get_prewhere_sql()`,
`get_limit_by_sql()` and `get_statement_end_sql()`, asked of a query only when it has such clauses.
`hare stubs` and the mypy plugin declare the methods of the dialects of a project's connections, with
the parameters of `apply` after the builder (and the query).

## <a id="transactions"></a>7. Transactions and concurrency

`Features.supports_transactions` decides whether transactions exist at all (see above).

The transaction itself is one state machine, written once in `TransactionClient`
(`hare.dialects.base.client`): `begin()`, `commit()`, `rollback()`, `savepoint()`,
`release_savepoint()` and `savepoint_rollback()`, nesting, the `on_commit()`/`on_rollback()`
callbacks, shielding a `COMMIT` from task cancellation, refusing a statement after the transaction
ended, and reporting `TransactionEvent`s. A driver's transaction client only supplies the
primitives it calls:

| Primitive | Does |
|---|---|
| `_driver_begin()`, `_driver_commit()`, `_driver_rollback()` | Start the transaction on the connection; send its `COMMIT`; send its `ROLLBACK`. |
| `_driver_savepoint(name)`, `_driver_release_savepoint(name)`, `_driver_rollback_to_savepoint(name)` | Open, release and roll back to a savepoint. |
| `_get_new_savepoint_name()` | A savepoint name not used on this connection yet. |
| `_is_connection_lost(error)` | Whether a driver error of a `COMMIT`/`ROLLBACK` means the connection is gone — the server rolls such a transaction back. `False` by default. |
| `_is_commit_outcome_unknown(error)` | Whether a lost connection leaves it unknown if a `COMMIT` in flight landed. |
| `_is_commit_rejection(error)` | Whether the database answered the `COMMIT` with an error — the transaction is over. |
| `_is_transaction_finished_error(error)` | Whether the driver reports that an earlier, interrupted `COMMIT`/`ROLLBACK` already ended the transaction. |
| `_check_commit_allowed()`, `_check_savepoint_allowed()`, `_before_top_level_end(event)`, `_after_rejected_commit(error)` | Optional hooks around the state machine's steps — SQLite refuses a `COMMIT` of an aborted transaction and switches a read-only one back before it ends. |
| `_take_transaction_resources()`, `_give_back_transaction_resources()` | Take what a top-level transaction holds for its whole life before `BEGIN` — a pooled connection, the connection's lock — and give it back as soon as the `COMMIT`/`ROLLBACK` landed, before the callbacks run. Nothing by default. |
| `_undo_failed_begin()` | End a transaction whose `BEGIN` raised but may have landed. Nothing by default. |
| `_end_unfinished_transaction()` | End what a top-level transaction left open on the driver once its block exits. Nothing by default. |

The six `_driver_*` methods and `_get_new_savepoint_name()` are abstract; the rest have defaults. A
nested transaction is a client of the same class sharing the connection — the base class makes it.

A client applying a transaction's `lock_timeout` itself — SQLite's busy timeout, the waits for
ClickHouse's row locks — sets `enforces_lock_timeout_itself = True`, and a database without a
statement for it then takes the option.

The dialect's `TransactionStatements` (`build_transactions()`) set a transaction up:

| Member | Meaning |
|---|---|
| `get_isolation_level(requested)` | The level a transaction asking for one runs at — the weakest of `Features.isolation_levels` at least as strong. |
| `get_isolation_level_sql(level)` | The statement setting it; None where every transaction runs at that level. |
| `get_read_only_sql()` | The statement making the transaction read-only (ISO `SET TRANSACTION READ ONLY` by default). |
| `get_begin_sql()`, `get_commit_sql()`, `get_rollback_sql()` | The statements opening, committing and rolling back a transaction (ISO `START TRANSACTION`, `COMMIT`, `ROLLBACK` by default) — written around the SQL `sqlmigrate` prints for an atomic migration, and sent where hare ends a transaction past its own callbacks. |
| `get_statement_timeout_sql(milliseconds)`, `get_lock_timeout_sql(milliseconds)` | Transaction-local statement and lock timeouts; None where the database has none. |

The schema editor's `TableLocks` part (`table_locks_class`): `get_lock_table_sql()` locks a table for the transaction and
`get_migration_lock_sql()` serializes concurrent `migrate` runs; `build_two_phase_commit()` gives
`Transactions.distributed()` its statements.

## <a id="schema-editor"></a>8. DDL — the schema editor

`build_schema_editor_class()` returns a `BaseSchemaEditor` subclass. The editor runs the public
operations (`create_model()`, `add_field()`, `alter_field()`, `add_index()`, ...) as the order of
their steps; the statements of each type of change are written by its **parts** — `SchemaEditorPart`
subclasses of `hare.dialects.base.schema.<folder>`, made once per editor from the classes it
declares and reaching the editor as `self.editor`:

| Folder | Parts |
|---|---|
| `columns/` | `ColumnDefinitions`, `ColumnTypeChanges`, `ColumnBackfill`, `ColumnNarrowingCheck` |
| `tables/` | `TableCreation`, `TableRebuild`, `TableComments`, `TablePartitions` |
| `relations/` | `ForeignKeyRebuild`, `ManyToManyThroughTables` |
| `indexes/` | `IndexStatements`, `GeneratedIndexNames` |
| `constraints/` | `ConstraintStatements`, `ConstraintNames` |
| `triggers/` | `TriggerStatements` |
| `schema_objects/` | `Views`, `MaterializedViews`, `DatabaseFunctions`, `Sequences`, `RowLevelSecurityPolicies`, `Grants`, `EnumTypes`, `Extensions`, `Schemas` |
| `runtime_statements/` | `TableLocks`, `TableClearing`, `TenantConditions` |

A dialect writes a part its own way by subclassing it and naming the subclass on its editor — the
attribute is the part's name in snake case with `_class`, the part itself the name without it:

```python
class ColstoreTableComments(TableComments):
    editor: ColstoreSchemaEditor

    def get_table_comment_sql(self, table, comment):
        literal = self.editor.client.dialect.literals.get_string_literal_sql(comment)
        return f"ALTER TABLE {self.editor.quote(table)} MODIFY COMMENT {literal}"


class ColstoreSchemaEditor(BaseSchemaEditor):
    table_comments_class = ColstoreTableComments    # editor.table_comments
```

The base renders standard DDL from the editor's class templates (`TABLE_CREATE_TEMPLATE`,
`FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`, `FOREIGN_KEY_TEMPLATE`, `ADD_FIELD_TEMPLATE`,
`ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); a dialect overrides the ones its database
writes differently and sets a template to `None` where the statement doesn't exist — the change then
rebuilds the table (`TableRebuild.remake_table()`: a new table from the model, the rows copied, the
old one replaced). `TableComments.get_table_comment_sql()` and `TableComments.get_column_comment_sql()`
write comments; `ColumnNarrowingCheck.get_decimal_overflow_predicate_sql()` finds the stored numbers
an `AlterField` to a narrower decimal can't hold.

`Features` steer what DDL is written at all:

| Feature | Without it |
|---|---|
| `supports_foreign_keys` | No `FOREIGN KEY` constraints; hare runs every `on_delete` action and `PROTECT` check itself, as for a `db_constraint=False` relation. |
| `supports_unique_constraints` | No unique constraints; a unique `Index` is a plain index; an upsert targets the primary key only. |
| `supports_adding_constraints` | CHECK constraints go into `CREATE TABLE`, unique ones become unique indexes, a later change rebuilds the table. |
| `supports_partial_indexes`, `supports_index_nulls_order`, `supports_concurrent_indexes` | The index part is left out or rejected. |
| `supports_exclusion_constraints`, `supports_deferrable_constraints`, `supports_not_valid_constraints` | The constraint type is rejected. |
| `supports_statement_triggers`, `supports_extensions`, `supports_collations` | `FOR EACH STATEMENT` triggers, `CreateExtension`, `CreateCollation` are rejected or skipped. |
| `truncates_values_on_type_change` | Set it when a narrowing type change silently cuts data — hare checks the data first. |
| `alters_indexed_columns` | Clear it when the database refuses to change a column an index covers — hare drops the covering indexes around the change and creates them again. |

A feature only says the database has it; the base editor writes none of its syntax. The dialect that
sets one writes the SQL in its parts — the clauses below are class methods of a part, which the core
calls as `dialect.schema_editor_class.<part>_class.<method>()` where it has no editor:

| Feature | What the dialect's parts implement |
|---|---|
| Non-key index columns (`include=`) | `IndexStatements.get_index_include_sql(quoted_columns)` — the clause after the index keys; `""` by default, which leaves the columns out. |
| `UniqueConstraint(nulls_distinct=...)` | `ConstraintStatements.get_nulls_distinct_sql(nulls_distinct)` — the clause; also set `Features.supports_nulls_distinct`. |
| Exclusion constraints | `ConstraintStatements.get_exclusion_constraint_extension(constraint, fields_by_name)` — the extension a constraint needs; `ConstraintStatements.exclusion_constraint_sql(model, constraint)` — its definition. |
| `supports_concurrent_indexes` | the editor's `add_index(model, index, concurrently)` and `remove_index(...)`; the base builds and drops the plain way. `IndexStatements.get_index_create_sql()` and `IndexStatements.get_index_drop_sql()` give the plain statements to start from. |
| `supports_not_valid_constraints` | `ConstraintStatements.add_check_constraint_not_valid()` and `validate_constraint()`; the base adds the constraint as usual and validates nothing. |
| `supports_extensions` | `Extensions.create_extension()`, `drop_extension()` and `get_extension_create_sql()`. |
| `supports_enum_types` | `EnumTypes.get_enum_type_create_sql()`, `drop_enum_type()` and `alter_enum_type()` — the `ENUM` types of `NativeEnumField` columns. |
| `supports_views` | `Views.get_view_create_sqls()`, `drop_view()` and `rename_view()`; the base `alter_view()` drops the view, creates the new one and grants on it again. |
| `supports_materialized_views` | `MaterializedViews.get_materialized_view_create_sqls()` (with the unique index of `unique_columns`), `drop_materialized_view()`, `rename_materialized_view()` and `refresh_materialized_view()`. |
| `supports_database_functions` | `DatabaseFunctions.get_function_create_sqls()`, `drop_database_function()`, `alter_database_function()` and `rename_database_function()`. |
| `supports_sequences` | `Sequences.get_sequence_create_sqls()`, `get_sequence_owner_sqls()`, `drop_sequence()`, `alter_sequence()`, `rename_sequence()` and `get_next_sequence_value()`. |
| `supports_row_level_security` | `RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`, `alter_policy()` and `rename_policy()`; for tenancy by row level security also `TenantConditions.get_tenant_condition_sql(quoted_column, column_type)` (the predicate of `TenantCondition`), `TransactionStatements.get_tenant_setting_sql(tenant_texts)` (the statement after `BEGIN` setting the transaction's tenants) and the connection option `tenant_row_level_security`. |
| `supports_grants` | `Grants.get_grant_sqls()` and `get_revoke_sqls()`. |
| `supports_collations` | `Extensions.create_collation()` and `drop_collation()`. |
| `supports_schemas` | `Schemas.move_table_to_schema()`, for a model whose `Meta.schema` changes. |
| Deferrable constraints | `TableClearing.defer_cascade_foreign_keys(model, connection)` — defers the foreign keys a cascade runs into. |
| Test databases | `TableClearing.clear_tables(connection, quoted_tables)` — empties tables between tests (one script of `DELETE FROM` per table by default). |

The same goes for lookups only one database has: the dialect registers them from its `install()`
with [`Field.register_lookup()`](custom-lookups.md), naming
itself in `dialects=` and the extension they need in `required_extension=` — as PostgreSQL does
for its trigram lookups.

### <a id="table-options"></a>Table options

A database whose `CREATE TABLE` takes more than columns declares a `TableOptions` subclass; models
list it in `Meta.table_options`, one entry per dialect, and a connection uses its own dialect's
entry:

```python
@dataclasses.dataclass(frozen=True)
class ColstoreTableOptions(TableOptions):
    dialect_name: ClassVar[str] = "colstore"

    engine: str = "MergeTree()"
    order_by: tuple[str, ...] = ()
    partition_by: RawSQLTerm | None = None

    def raise_if_unsupported(self, model):
        if not self.order_by:
            raise ConfigurationError(f"{model.__name__}: ColstoreTableOptions needs order_by")

    def get_create_suffix_sql(self, model, quote):
        sql = f" ENGINE = {self.engine} ORDER BY ({', '.join(quote(column) for column in self.order_by)})"
        return sql + (f" PARTITION BY {self.partition_by.sql}" if self.partition_by else "")


class Event(Model):
    class Meta:
        primary_key = None
        table_options = [ColstoreTableOptions(order_by=("created_at",))]
```

The dialect names the class in `build_table_options_class()`. Migrations keep the options in the
model state and write them into migration files through `deconstruct()`; a change of them is applied
by the `TablePartitions` part's `alter_table_options()` — by default the table is rebuilt with the new
options. `hare drift` compares them with what the introspector reads, and `inspectdb` writes them
into `Meta.table_options` (see the introspector below).

Options that hold parts of the table the migrations add and remove one at a time — partitions —
take four more hooks, all with a default that means "none":

| Hook | What it gives |
|---|---|
| `get_partitions()` | The partitions by name; each partition object names its dialect as `dialect_name` and has a `name` and a `deconstruct()`. |
| `with_partitions(partitions)` | The options with another set of them. |
| `can_change_partitions_to(new_options)` | Whether the other options are reached by adding and removing partitions — else the change is one `AlterModelOptions`. |
| `with_field_names(column_to_field_name)` | The options naming model fields where they name columns — options read from a database name columns. |

Options storing the rows elsewhere than in the model's own table — a table spread over the
servers of a cluster — take three more:

| Hook | What it gives |
|---|---|
| `get_storage_table_name(table_name)` | The table the DDL of the model's columns and storage is written for — the model's own by default. |
| `get_companion_table_sqls(model, quote, client, replaces=...)` | The statements creating the table put before the one storing the rows; none by default. |
| `keeps_row_versions()` | Whether the table keeps several rows of one key that the database merges — the key is then not checked (`checks_constraints_before_write`). |

The table rebuild's `copy_table_rows(model, new_table_name, old_table_name, column_mapping)` copies
the rows into the rebuilt table — one `INSERT ... SELECT` by default. The dialect's
`get_journal_table_options(connection)` stores the journal of applied migrations (ClickHouse's on a
cluster is replicated), and `synchronize_table(connection, table_name)` waits for the rows other
servers wrote to it before the journal is read.

`raise_if_unsampled(model)` rejects a `sample()` of a table its options allow none of — ClickHouse's
without `sample_by`; nothing by default. A dialect writing the primary key of a table elsewhere than
among its columns (ClickHouse, in its engine's `PRIMARY KEY` clause) returns `""` from its table
creation part's `get_composite_pk_constraint_sql()` and leaves `PRIMARY KEY` out of its column
definitions part's `get_field_sql()`.

`makemigrations` then writes `AddPartition`/`RemovePartition` for each partition that came or went,
and they call the `TablePartitions` part's `add_partition(model, partition)`/`remove_partition(model,
partition)` (`UnSupportedError` by default); `TablePartitions.get_partition_create_sqls(model, safe)` returns the
statements creating the partitions right after the table's `CREATE TABLE`. PostgreSQL's
[partitioned tables](../models/meta-options.md#partitioning) are built on these hooks.

### <a id="models-without-a-primary-key"></a>Models without a primary key

A table of a columnar database often has no primary key: `Meta.primary_key = None` (see
[Meta options](../models/meta-options.md#primary_key)). Reads, filters, aggregates,
`bulk_create()` and `QuerySet.update()`/`delete()` work; what needs a key to identify a row
(`save()` of a fetched row, `instance.delete()`, relations to the model, ...) raises
`ConfigurationError`.

## <a id="introspector"></a>9. Reading an existing schema — the introspector

`build_introspector_class()` returns a `SchemaIntrospector` subclass implementing
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` and
`fetch_tables(connection, tables, schema) -> list[TableInfo]`; `fetch_schema_names(connection)` lists the schemas for a
[schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md) (`UnSupportedError` by default). The rest — `inspectdb`, drift, the
reverse type mapping to fields — works from the neutral `TableInfo`/`ColumnInfo`/`IndexInfo`/
`ForeignKeyInfo`. A table's storage goes into `TableInfo.table_options` through
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, which keeps the names the
class declares and gives None when every option has its default; `fetch_declared_table_options()`
lets drift take a declaration the database records another way as the same storage, and
`fetch_declared_schema_objects(connection, schema, declared, table_info, column_to_field_name)`
returns the views, materialized views and dictionaries a model declares as the database has them —
as declared by default, the database not asked.

How the database echoes an expression back is the introspector's too: `NOW_EXPRESSIONS` (the
spellings of the current time), `SEQUENCE_DEFAULT_PREFIXES` (a default drawing a sequence's next
value), `split_type_cast(sql)` (the type cast ending an expression, split off — PostgreSQL's
`'active'::character varying`), `NUMERIC_CAST_TYPES` (the casts whose quoted literal is a number) and
`strip_type_casts(sql)` (every cast left out, for comparing a declared default with the one read
back). The defaults read nothing of the type. Without an introspector (`None`, the default)
`inspectdb` and `hare drift` raise `UnsupportedDialectError` (`hare.inspectdb.exceptions`, an `UnSupportedError`).

## <a id="migrations"></a>10. Migrations

The migration journal and every operation go through the schema editor, so they need nothing of
their own. `Features.can_rollback_ddl` decides whether a migration runs in a transaction, the
`TableLocks.get_migration_lock_sql()` whether two `migrate` runs wait for each other, and the
renderers' `get_connection_only_function(sql)` names a function hare installs on its own
connections that DDL can't use (the SQLite functions hare registers on each connection).

`ForeignKeyRebuild.add_foreign_key_constraint_not_valid()` and
`ConstraintStatements.add_unique_constraint_using_index()` back `AddField(..., not_valid=True)` and
`AddConstraint(..., using_index=...)`; the base adds the constraint checked, and builds the
constraint's own index and drops the old one. The class method
`ColumnTypeChanges.rewrites_table_on_alter()` tells which field changes rewrite the table — every
column type change by default.

The [migration safety check](../migrations/zero-downtime.md#checking-migrations) takes its rules from
`MigrationSafetyRules`: `get_rules()` lists them — the base gives the ones every database shares, a
dialect adds its own, each a `MigrationSafetyRule` subclass in a file of its own with a `code`
(`MigrationRiskCode`) and `check_operation(context)` (or `check_migration(contexts)` for a rule
about the whole migration) returning a `MigrationRisk` from `context.get_risk(...)`.
`count_table_rows(client, table_name, schema, at_most)` counts a table's rows up to `at_most` (an
estimate will do), 0 for a missing table; the base returns None — every table counts as large.

## <a id="without-guarantees"></a>11. What hare does for a database without guarantees

| The database lacks | hare |
|---|---|
| Transactions | Rejects `atomic()`; runs its own multi-statement writes statement by statement. |
| Savepoints | Joins a nested `atomic()` to its transaction, which rolls back as a whole if the nested block fails. |
| Generated keys | Refuses a model whose primary key the database generates when it is bound — or takes its keys from a series before the insert (`takes_keys_before_insert`). |
| `UPDATE` / `DELETE` of stored rows | Refuses each before it is sent; checks a whole cascade before its first write, so a database without transactions is never left with half of one. |
| Correlated subqueries | Writes an `EXISTS` correlated by equal columns as a membership test; refuses any other correlated subquery before it is sent. |
| Foreign keys | Runs `on_delete` and `PROTECT` in Python; with `checks_constraints_before_write`, checks the target of a relation before a write. |
| Unique constraints | Creates none; an upsert conflicts on the primary key only — with `checks_constraints_before_write`, the declared uniqueness is checked before a write and an upsert reads the conflicting rows first. |
| `RETURNING` | An `UPDATE` doesn't read changed generated columns back; a key the database generates isn't read onto the instance; `db_default` values are read with a `SELECT` after the `INSERT` — with `returns_rows_by_reading`, `returning()` reads the rows by their keys. |
| Row locks | Rejects `select_for_update()` — or locks the rows by their keys through the transaction client (`locks_rows_by_key`). |
| Deferrable constraints | `TableClearing.defer_cascade_foreign_keys()` raises `UnSupportedError` — a hard delete through a `PROTECT` guard in the same cascade can't be deferred. |
| A primary key | Supports keyless models as above. |

`Features.checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` and
`cascade_depth_limit` describe how a native cascade behaves, so hare knows when to defer or run it
itself. A client whose features set `cascade_depth_limit` raises `CascadeDepthLimitError` from
`hare.exceptions` — or a subclass of its own, as SQLite's `SqliteTriggerRecursionLimitError` — when
the database stops a cascade at that depth; `delete()` catches it, rolls the attempt back and walks
the cascade in Python.

## <a id="testing"></a>12. Testing

hare's own suite runs against a dialect: install the package (its entry point registers the
driver) and point `HARE_TEST_DB` at it (`colstore+colstore-client://.../test_{}`, the `{}` filled per run) — every
test the dialect's features allow runs. A test of something a database may lack is marked with the
features it needs:

```python
from hare.contrib.test import requires_features

@requires_features(supports_transactions=True)
async def test_rollback(db): ...

@requires_features(supports_foreign_keys=True)
async def test_cascade_in_the_database(db): ...
```

`tests/test_dialect_contract.py` checks every registered dialect and driver: names, schemes,
features, every part built for its own dialect, the registries, the schema editor, the introspector
and deterministic SQL. A test database is reset between tests with
`TableClearing.clear_tables()`.

## <a id="observability"></a>13. Observability and speed

`otel_system_name` is the OpenTelemetry `db.system.name` value (`postgresql`, `clickhouse`, or `other_sql` for a database OpenTelemetry names none for). The Rust
row readers depend on the client's `native_python_types`, not on the dialect's name. Every statement
the client runs reaches [`Observers`](../observability/observers.md) — `QueryExecuted`, query
wrappers, slow-query logging — through the decorator described in [the client](#client); a client
that skips it is invisible to `capture_queries`, the N+1 detector and OpenTelemetry.

## <a id="checklist"></a>Checklist

- [ ] `Dialect` subclass: name, `otel_system_name`, `features`, and the parts the database writes
      differently — `build_literals()`, `build_parameters()`, `build_renderers()`,
      `build_clauses()`, `build_transactions()`, `build_types()`, `build_filter_operators()`.
- [ ] Query class with `SQL_CONTEXT = dialect.sql_context`.
- [ ] Client: statements, error translation, `features` from the dialect's plus the driver's;
      transactions where the database has them.
- [ ] Driver: name, schemes, credentials, client classes; registered on import.
- [ ] `hare.dialects` entry point.
- [ ] Schema editor templates and class methods, table options, introspector — as the database needs.
- [ ] hare's suite green under `HARE_TEST_DB`, including `tests/test_dialect_contract.py`.
