# Writing a dialect: the driver and the client

Steps 1 and 2 of [Writing a dialect](index.md): connecting through the database's driver, and the client
that runs statements on it.

## <a id="driver"></a>1. Connecting: the driver

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
| `url_schemes`, `path_credential`, `authority_credentials`, `default_credentials`, `url_has_userinfo` | How a database URL becomes the client's keyword arguments. |
| `connection_options`, `strict_query_parameters` | The settings a connection takes, each declared once as a `ConnectionOption` — see [`connection_options`, `strict_query_parameters`](#connection-options-strict-query) |
| `get_client_class(credentials)` | The client class for a connection — it may pick one by a credential and remove it (SQLite's `install_regexp_functions`). |
| `get_client_classes()` | Every client class, transaction clients included. |
| `is_retryable(error)` | Whether a driver error means the database aborted the transaction for a concurrent one (serialization failure, deadlock); hare raises `TransactionRetryError` for it. |
| `get_url_path(url)`, `get_testing_path(path, reuse_databases)` | Reading the path of the database URL, and the path a test run connects to (the `{}` placeholder of `HARE_TEST_DB` filled with a fresh name). |

### <a id="connection-options-strict-query"></a>`connection_options`, `strict_query_parameters`

The settings a connection takes, each declared once as a
`ConnectionOption(name, value_type, *, minimum, maximum, positive, power_of_two, choices)` — its value
type is `ConnectionOptionType.WHOLE_NUMBER`, `SECONDS`, `BOOLEAN`, `CHOICE` or `TEXT`. A setting is
checked the same way whether it comes typed from a config dict's `credentials` or as text from a database
URL query parameter (`?connect_timeout=5`): the client calls `options.read(settings)` in its `__init__`
to take the known settings out, type- and range-checked, and
`options.raise_for_unknown(settings, driver_name)` to reject a misspelled one. No hand-written parsing.

## <a id="client"></a>2. Running statements: the client

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
`acquire_connection()` (a context manager handing out the raw connection) and the statement methods — the
whole contract between hare and a driver:

| Method | Runs |
|---|---|
| `execute(query, values=None, *, returns_rows=None, rows_by_position=False) -> StatementResult` | One statement — see [`execute()`](#execute) |
| `execute_many(query, values)` | One statement once per parameter row; returns nothing. |
| `execute_script(query)` | A script of several statements the user wrote (`execute_script()`, `RunSQL`), passed on verbatim. A script of no statement — empty, or comments only — runs as nothing on every built-in driver. |
| `execute_built_script(query)` | A script hare built — its schema statements — as the connection runs them; `execute_script()` by default. ClickHouse's client sends each statement of it over the connection's cluster (`ON CLUSTER`), which a user's script never gets. |
| `execute_described(query, values=None) -> DescribedResult` | One statement with the names of its columns (`columns`, `rows` as tuples, `row_count`) — for showing any result as a table. |
| `copy(table, columns, records, column_types)` | A bulk load, where the database has such a protocol (`Features.supports_copy`). It is reported to the observers under the dialect's statement of it — `QueryClauses.get_bulk_load_statement_sql()`. |
| `_driver_stream_batches(query, values, chunk_size)` | An async generator of batches of rows (lists) over a server-side cursor, where `Features.supports_streaming` — on the transaction client; `chunk_size` 0 is the driver's own batch size. The public `stream_batches()` and `stream()` are the base class's: they tag, wrap and report the query around it. |

### <a id="execute"></a>`execute()`

One statement. `StatementResult(row_count, rows, inserted_id=None, description=None)`
(`hare.dialects.base.results`): the rows it returned — each read by column name — and the rows it changed
for a write without `RETURNING`. `returns_rows` says whether the statement returns rows (a `SELECT`, a
write with `RETURNING`); `None` leaves the driver to find out from the SQL text. `rows_by_position=True`
is passed by a reader taking the rows by position alone where `Features.supports_positional_rows`: the
driver may then give its cheapest rows read by position — plain tuples — with the database-API
`description` of the columns, which `StatementResult.column_names` reads (from the first row's names when
no description is given). hare passes it for its own reads and for `UPDATE`/`DELETE` statements counting
their rows. Every read and write of the ORM goes through it — there is no separate insert or
dict-returning method to implement (`execute_dicts()` is built on it; set `row_to_dict` when the driver's
rows convert faster than `dict(row)`).

A statement's `values` are a `list`, or its subclass `QueryParameters`
(`hare.sql.terms.parameters.query_parameters`) when some of them hold a `sensitive=True` field's value:
`repr()` of it shows those as `<hidden>`, so a client logging its parameters with `%s` hides them without
doing anything. A driver that accepts no list subclass is given `list(values)`.

`native_python_types` is the set of Python types the driver hands back as a field wants them — the row
readers skip conversion for a field of such a type.

Each statement method turns the driver's own exceptions into hare's — `DBConnectionError`,
`IntegrityError`, and `OperationalError` or `TransactionRetryError` through `_get_operational_error()`,
which asks the driver's `is_retryable()` — and reports the call to
[observers and query wrappers](../../observability/observers.md). Both happen in one decorator,
`DatabaseClient.translate_exceptions`, which the dialect's client class applies to the statement methods
of its drivers (`@ColstoreClient.translate_exceptions`). It appends the active query tags
(`DatabaseClient.get_tagged_query_arguments()`), runs the call inside the query wrappers when any is
installed (`Observers.run_wrapped(QueryCall(...), proceed)`), reports it to the observers once it has
finished, successful or not (`Observers.record_query()`), and raises what the driver raised as the
client's `translate_driver_error(client, error, sql, parameters, args, is_query_executing)` returns — the
one method a dialect's client overrides to map its driver's exceptions. The hooks the decorator reads are
class attributes of the client, read once when a method is wrapped: `rejected_sql_character` (a character
refused before the statement is sent), `checks_aborted_transactions` and `aborted_transaction_message` (a
statement refused in a transaction the database has aborted), `runs_statement_options`
(`command_timeout`, `password_provider`, a transaction pooler, read retries). The decorator runs on every
statement, so it reads the client's `is_transaction_client` (true on every `TransactionClient`) rather
than calling `isinstance()`.

Once a connection opens, the client calls `_post_connect()`, which reads the server's version from
`get_server_version()` — `(major, minor, ...)`, read without query instrumentation; the default `None`
skips the check. A server older than `Dialect.minimum_server_version` is refused with `UnSupportedError`
and the connection closed; `Dialect.get_server_version_features(version)` returns the `Features` values
the version changes (PostgreSQL turns `supports_nulls_distinct` off before 15 and leaves out the
`EXPLAIN` options an older server lacks), applied to that connection only. A transaction wrapper shares
its connection's `features`.

A database with transactions implements `_get_transaction_client()` — a new client of the driver's
`TransactionClient` subclass, which gives the driver's primitives (see
[Transactions and concurrency](transactions.md#transactions)). The transaction context around it is
hare's own: it takes the client's resources, begins, and commits or rolls back on exit. The statements
run right after `BEGIN` — isolation level, read only, statement and lock timeouts — come from the
dialect's `TransactionStatements`; a client that cancels an over-running statement itself, without a
statement of the dialect's, sets `enforces_statement_timeout_itself = True` (SQLite's interrupts the
query).

### <a id="client-hooks"></a>Hooks with a default

Every other public method of `DatabaseClient` works for any driver as it is; a dialect overrides one
where its database or driver does something of its own.

| Method | Does | By default |
|---|---|---|
| `acquire_from_open_pool() -> (pool, connection)` | Checks out one connection of the pool, with the pool it came from — a pooled client retries on the new pool when `Connections.reconnect()` closed the one it waited on. | The pool and a connection of it. |
| `abandon_connections()` | Drops the connections opened on an event loop that is already closed — they can't be closed through it. | Nothing: a driver whose connections need no event loop to close. |
| `check_stream_open()` | Refuses to hand out a streamed row the connection no longer reads. | Nothing: a stream outside a transaction is read to its end or closed. |
| `get_result_read_error(error)` | The hare exception of an error the driver raises reading a value after the statement returned. | `None`: a driver decoding every value before the statement returns. |
| `is_unencodable_surrogate_error(error)` | Whether the driver failed to encode a text holding a lone surrogate. | Python's own error, or the driver's caused by it. |
| `raise_rejected_sql_character(sql)` | Raises for a statement holding `rejected_sql_character`. | Must be implemented by a client setting `rejected_sql_character`. |
| `enforces_foreign_keys()` | Whether the database enforces foreign keys on this connection now — a hard delete cascades in Python where it doesn't. | `Features.supports_foreign_keys`. |
| `get_address()` | The server's address for reports (`ConnectionFailed`). | Empty. |
| `get_pool_bounds() -> (least, most)` | The connections the pool keeps, as configured — for `get_pool_status()` of a pool failing to open. | Must be implemented with `Features.supports_pool_status`. |
| `ping(timeout=5.0)` | `SELECT 1`, bounded by `timeout`; `False` for a connection error, an operational error or a timeout. | The same for every driver. |
| `create_independent_client(settings=None)` | A new connection to the same database, apart from this one and in the same tenant schema; the caller closes it. | Built from the connection's settings. |
| `get_server_client()` | The client of the server itself, past the pooler the connection goes through (`direct_host`). | This client. |
| `lock_timeout_session(seconds)` | A client on one connection whose every statement waits at most `seconds` for a lock — for a migration running outside a transaction. | Raises `UnSupportedError`. |
| `get_tenant_client()` | The client the active tenant scope works through — of the tenant's schema with a schema per tenant. | This client, or the schema's client. |
| `get_schema_client(schema_name)` | The client of one tenant's schema, made on its first use. | Raises `UnSupportedError` without schemas per tenant. |
| `enter_tenant_schema(schema_name) -> state` / `leave_tenant_schema(state)` | Runs the rest of the transaction in another tenant's schema, and comes back — a delete reaching the rows of every tenant schema. | Raise `UnSupportedError` without schemas per tenant. |
| `close_tenant_clients()` | Closes the clients of the tenant schemas. | Closes each schema client opened. |

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
  connection settings and their checks, the transaction lifecycle (begin, commit, rollback, savepoints,
  aborted transactions), error translation, the pool status. It never imports a driver library. Where it
  needs a fact of the library — the exception classes, the library's own version and known faults — it
  reads a class attribute the driver's client sets: SQLite's clients set
  `driver_errors = SqliteDriverErrors(...)`, read only inside an `except` clause, so a statement that
  succeeds pays nothing for it.
- **A driver's client** subclasses it and implements what the library does: `create_connection()`,
  `acquire_connection()`, the statement methods, cursors and streaming, the transaction primitives
  (`_driver_commit()`, `_driver_savepoint()`, ...), `get_server_version()`, and the `Features` the
  library adds (`AiosqliteClient.features` adds the bind parameter limit of the `sqlite3` build). Its
  transaction client inherits the shared transaction client first:
  `AiosqliteTransactionClient(SqliteTransactionClient, AiosqliteClient)`.
- **The driver** keeps one name, its database URL scheme `<dialect>+<driver>`: `sqlite+aiosqlite://`,
  `postgresql+asyncpg://`, `clickhouse+clickhouse-connect://`, `clickhouse+clickhouse-driver://`. The
  dialect's plain scheme belongs to hare's own engine of the dialect — `postgresql://` (rust_pg); no
  other driver takes it.
- **The pool.** A client whose pool lives in Python counts in hare's `PoolStatistics`. A driver whose
  pool lives outside Python returns its own object with the same methods from `create_pool_statistics()`,
  and reports the occupancy from `get_pool_occupancy()` (with `Features.supports_pool_status`).
- **A native driver.** The parts of `rust.native` every native driver shares are outside any one
  database: `rust.native.pool` (`PoolStatistics`, `set_pool_metrics_enabled()`, the counted checkout of a
  deadpool pool) and the asyncio bridge its futures resolve through. A new native driver for SQLite or
  ClickHouse uses them as `rust.native.pg` does and is one more driver of the dialect — the shared client
  stays.

### <a id="password-provider"></a>Rotating passwords

A client whose database takes passwords that change supports `password_provider` the way PostgreSQL's
does ([Rotating credentials](../../dialects/postgresql/connecting.md#rotating-credentials)):

- Its settings include `PASSWORD_PROVIDER_OPTIONS` (`hare.dialects.base.connection.constants`) —
  `password_provider` and `password_refresh_seconds`, checked like any setting. A dialect without them
  refuses both as unknown settings.
- The client sets `self.password_provider = PasswordProvider.from_settings(settings, password)`
  (`hare.dialects.base.client.password_provider`) — None without a provider, `ConfigurationError` for a
  provider next to a fixed password.
- A driver that opens each connection itself asks `await self.password_provider.get()` there — the cached
  password, asked again once older than the refresh. A driver whose pool opens connections on its own
  overrides `apply_password(password)` to hand the pool a new password, starts
  `password_provider.start_refreshing(self.apply_password)` once the pool is open and stops it with
  `await password_provider.stop_refreshing()` when the pool closes.
- When the server refuses a password, `await client.renew_password()` asks the function right away and
  applies the answer; a client runs the refused statement once more only when it knows the statement
  never reached the server.

### <a id="shell-command"></a>The interactive client

`hare dbshell` runs what `await client.get_shell_command()` returns — a `ShellCommand`
(`hare.dialects.base.client.declarations`): the program with its arguments, and the variables added to
its environment. A password goes into the environment, never among the arguments other users of the
machine see. The default `None` makes `hare dbshell` refuse the dialect; a connection no other program
can open (an in-memory database) raises `UnSupportedError` with the reason.

### <a id="features"></a>Features

`Features` (`hare.dialects.base.features`) are the one place hare asks what a database and its driver
support. The dialect declares what the database supports (`Dialect.features`), the client starts from
them and sets what its driver adds, and the server version adjusts them per connection. The core reads
`connection.features` where it has a connection and `dialect.features` where it has only the dialect (DDL
rendered for a migration file, a lookup list).

| Feature | hare's reaction when it's off |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` raise `UnSupportedError`; hare's own multi-statement writes (a cascade, an M2M `add()`, batched `bulk_create()`) run their statements one by one. |
| `supports_savepoints` | A nested `atomic()` joins the transaction it is nested in; an error leaving the nested block makes the transaction roll back as it ends and raise `TransactionManagementError`, even if the outer block caught the error. |
| `supports_generated_keys` | A model whose primary key the database generates (`IntField(primary_key=True)` by default) is refused with `ConfigurationError` when bound to the connection — give it a key the application sets (`UUIDField(primary_key=True, default=uuid.uuid7)`). hare's own migration journal is keyed by `(app, name)`. |
| `takes_keys_before_insert` | Set it where the keys come from a series before the rows are written: hare calls the client's `take_generated_keys(model, count)` for the rows without a key and writes them with their keys; `synchronize_key_series(model)` moves the series past the table's greatest key (`SynchronizeKeySeries`). |
| `supports_unique_constraints` | A model declaring a uniqueness (`unique=True`, `UniqueConstraint`, `Index(unique=True)`) is refused with `ConfigurationError` when bound; a migration declaring one is refused with `UnSupportedError` before its first statement. |
| `supports_foreign_keys` | hare runs every `on_delete` itself, and a relation with `db_constraint=True` — a many-to-many relation's automatic through table too — is refused with `ConfigurationError` when its model is bound. |
| `references_other_schemas` | A relation with `db_constraint=True` to a model of another schema is refused with `ConfigurationError` when its model is bound. |
| `reports_written_row_count` | The client's `StatementResult.row_count` of a write is `None`: `update()`, `delete()` and `bulk_update()` return `None`, a write of a row that is gone goes unnoticed, and a model with `Meta.optimistic_lock_field` is refused with `ConfigurationError` when bound. |
| `supports_insert_conflicts` | `bulk_create(ignore_conflicts=..., update_fields=..., on_conflict=..., on_conflict_constraint=...)` raises `UnSupportedError` before any SQL is sent. |
| `supports_row_updates` | Every `UPDATE` — `save()` of a stored row, `QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` of a cascade — raises `UnSupportedError` before it is sent, and a cascade needing one writes nothing — see [`supports_row_updates`](#supports-row-updates) |
| `supports_row_deletes` | Every `DELETE` raises `UnSupportedError` before it is sent, and a cascade needing one writes nothing. |
| `rewrites_correlated_exists` | Set it where the database runs correlated subqueries but computes a correlated `EXISTS` wrongly: such an `EXISTS` keeps its `IN` form. |
| `supports_ordered_correlated_subqueries` | A correlated subquery ordering or slicing its own rows raises `UnSupportedError` before the statement is sent. |
| `orders_by_correlated_subqueries` | A query with a correlated subquery in `ORDER BY`, or selected beside a `WHERE`, selects its rows in a derived table — the conditions and sort keys hidden columns of it — and filters, orders and slices them outside; an `UPDATE`/`DELETE` with a correlated subquery raises `UnSupportedError`. |
| `supports_correlated_subqueries` | An `EXISTS` correlated by equal columns is written as `(outer columns) IN (SELECT inner columns ...)` — see [`supports_correlated_subqueries`](#supports-correlated-subqueries) |
| `can_rollback_ddl` | A migration isn't wrapped in a transaction. |
| `supports_select_for_update` | `select_for_update()` and `RowChanges.get_for_update()` raise `UnSupportedError` when the query runs; `update_or_create()` skips its lock. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` takes a plain `FOR UPDATE` lock. |
| `supports_select_for_share` / `supports_select_for_key_share` | `select_for_update(share=True)` / `(key_share=True)` raises `UnSupportedError` when the query runs. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` of a sliced queryset raise `QueryError` before any SQL is sent; with it, a slice without an offset is written as the statement's own `ORDER BY ... LIMIT`. |
| `supports_returning` | An `INSERT` asks for nothing back, and `returning()` and `bulk_create(returning=True)` raise `UnSupportedError` — see [`supports_returning`](#supports-returning) |
| `guarantees_returning_order` | A multi-row `INSERT ... RETURNING` is matched back to its objects by primary key instead of by position. |
| `supports_posix_regex` | A filter with `posix_regex`/`iposix_regex` raises `UnSupportedError` before the query runs. |
| `cuts_like_pattern_at_null_byte` | Set it where `LIKE` reads a pattern only up to its first null byte: a text lookup whose pattern holds one raises `ValidationError`. |
| `supports_two_phase_commit` | `Transactions.distributed()` rejects the connection. |
| `supports_listen_notify` | `NotificationListener.start()`/`run()` raise `UnSupportedError`, and the outbox's `ListenNotifyWakeup` raises `ConfigurationError` when it signals. |
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
| `max_bulk_update_rows` | `bulk_update()` writes at most this many rows per statement. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` raises `UnSupportedError` before its DDL is sent. |
| `supports_unhex` | A long `__in` of bytes binds one parameter per value instead of one JSON array. |
| `supports_drop_column` | A schema editor that rebuilds tables (SQLite's) rebuilds one for every removed field. |
| `explain_options` | An `EXPLAIN` option not listed raises `UnSupportedError` before the statement is sent. |
| `supports_schemas` | Migrations create and drop no schema; a schema-qualified model's table is written as it is and has to exist. The schema editor's `get_index_name_sql()`, `get_indexed_table_sql()` and `get_referenced_table_sql()` write the names an index, a trigger and a foreign key take in the database's own form. |
| `supports_distinct_on` | `distinct(*fields)` raises `UnSupportedError` when the query is built. |
| `supports_grouping_sets` | `group_by(Rollup/Cube/GroupingSets(...))` and `Grouping()` raise `UnSupportedError`. |
| `supports_lateral` | `Lateral(...)` raises `UnSupportedError`. |
| `supports_table_sample` | `QuerySet.sample(...)` raises `UnSupportedError`. |
| `supports_asof_join` | `AsofJoin(...)` raises `UnSupportedError`. |
| `supports_array_join` | `ArrayJoin(...)` raises `UnSupportedError`. |
| `supports_json_type` | ClickHouse's own: the `JSON` type, read by the connection's driver. |
| `supports_dictionaries` | `Meta.dictionaries` raises `UnSupportedError` before its DDL; else the schema editor's `dictionaries_class` (`Dictionaries`: `get_dictionary_create_sqls()`, `drop_dictionary()`, `alter_dictionary()`, `rename_dictionary()`, `reload_dictionary()`). |
| `supports_merge` | `QuerySet.merge()` raises `UnSupportedError`. |
| `supports_merge_returning` | `merge().returning()` raises `UnSupportedError`. |
| `supports_merge_not_matched_by_source` | `merge().when_not_matched_by_source()` raises `UnSupportedError`. |
| `supports_partitioned_exclusion_constraints` | An `ExclusionConstraint` on a partitioned model raises `UnSupportedError`. |
| `supports_pool_status` | `get_pool_status()` raises `UnSupportedError`, and the client's pools are left out of `Connections.get_pool_statuses()`. |
| `sorts_nulls_first` | Where NULL sorts by default — `nulls_first`/`nulls_last` add the clause only where it changes the order. |
| `divides_integers_exactly` | Set it where `/` of two integers gives the exact quotient: the expression's output field is a `FloatField` instead of the integer field. |
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
| `isolation_levels` | The levels a transaction runs at, weakest first — see [Transactions](transactions.md#transactions). |
| `max_identifier_length` | The most bytes a name may take, None for no limit. hare generates names of up to 63 bytes, shortening a longer one with a digest; a dialect with a lower limit can't be registered. |
| `cascade_depth_limit`, `checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` | How a native cascade behaves — see [without guarantees](guarantees-testing-observability.md#without-guarantees). |

<a id="supports-row-updates"></a>**`supports_row_updates`.** Every `UPDATE` — `save()` of a stored row,
`QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` of a cascade — raises `UnSupportedError`
before it is sent, and a cascade needing one writes nothing. A soft-deleted model
(`Meta.soft_delete_field`) is refused with `ConfigurationError` when bound; the outbox relay refuses to
start.

<a id="supports-correlated-subqueries"></a>**`supports_correlated_subqueries`.** An `EXISTS` correlated
by equal columns — a filter or an exclusion across a to-many relation, `<m2m>__isnull`, `Exists(...)`
with `OuterReference` equalities — is written as `(outer columns) IN (SELECT inner columns ...)` with
NULLs ruled out, true and false exactly where the `EXISTS` is. Any other correlated subquery
(`Subquery(...)` with `OuterReference`, an `OuterReference` compared other than by `=`) raises
`UnSupportedError` before the statement is sent; so does the outbox relay's start.

<a id="supports-returning"></a>**`supports_returning`.** An `INSERT` asks for nothing back: a key the
database generates comes the dialect's own way (SQLite's last row id, a series taken before the insert
with `takes_keys_before_insert`) — a primary key left to a `db_default` is refused when a row is written —
and the values of `db_default` and computed columns stay unread: the field holds
`DatabaseDefault`, a computed field isn't loaded, and `refresh_from_db()` reads them. An `UPDATE` doesn't
read changed generated columns back, a bulk write can't tell which rows it really inserted, and
`returning()` and `bulk_create(returning=True)` raise `UnSupportedError`.

The DDL features are listed with [the schema editor](schema.md#schema-editor). A client class can derive
another's features: `AiosqliteClient.features.replace(supports_posix_regex=True)`.

### <a id="executor"></a>Model-level statements

There is nothing per driver between a model and `execute()`: the same write pipeline builds every
`INSERT`, `UPDATE`, upsert and `DELETE` — `save()`, `bulk_create()`, `QuerySet.update()` alike — and the
same row readers build instances from what `execute()` returns. What differs between databases is asked
from the dialect's parts and the connection's `Features`: generated and database-default columns come
back through `INSERT ... RETURNING` where `supports_returning`; `clauses.get_upsert_inserted_flag_sql()`
gives the `RETURNING` expression that tells an inserted row of an upsert from an updated one
(PostgreSQL's `xmax = 0`; `None` makes hare read the existing keys before the write, and only while an
observer listens).
