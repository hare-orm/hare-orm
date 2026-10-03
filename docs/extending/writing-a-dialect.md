# Writing a dialect

hare talks to a database through two objects. A **dialect** is the database's language: its SQL
syntax, column types, DDL and catalog. A **driver** connects to it: DB_URL schemes, credentials and
the client class that runs statements. One dialect can have several drivers - PostgreSQL has
`asyncpg` and `rust_pg`.

A new database is added from a package of its own; nothing in hare changes. hare's test suite runs
against such a dialect too: `tests/dialects/columnar/` is a dialect built only from the public API
(numbered placeholders, backtick-quoted names, no transactions, foreign keys or unique constraints,
UUIDs stored as bytes, a `QuerySet` method and table options of its own), and the whole suite runs
green on it. Read it next to this guide.

This guide follows a ClickHouse-like database through every part - a columnar database without
transactions, foreign keys or unique constraints, with named placeholders, backtick quoting,
tables that need an engine and a sort key, and query modifiers (`FINAL`, `SAMPLE`) of its own.

## The parts {: #the-parts }

| Part | Base class | What it decides |
|---|---|---|
| Dialect | `hare.dialects.base.dialect.Dialect` | Syntax flags and hooks, type mappings, lookup operators, term renderers, schema editor, introspector. One instance, shared by every driver and connection. |
| Driver | `hare.dialects.base.driver.Driver` | Name (the connection config's `engine`), DB_URL schemes and credentials, the client class, retryable errors. |
| Client | `hare.dialects.base.client.DatabaseClient` | One connection: running statements, transactions, the `Features` it has. |
| Query class | `hare.sql.queries.Query` / `QueryBuilder` | Renders statements in the dialect's context; overrides a clause the database writes its own way. |
| Schema editor | `hare.dialects.base.schema.editor.BaseSchemaEditor` | DDL for `generate_schemas()` and migrations alike. |
| Introspector | `hare.inspectdb.introspector.SchemaIntrospector` | Reads an existing schema for `inspectdb` and `hare drift`. Optional. |
| Table options | `hare.ddl.table_options.TableOptions` | What the dialect's `CREATE TABLE` takes beyond columns. Optional. |

## Registering {: #registering }

The package's driver module registers the driver - and with it the dialect - when it is imported:

```python
# hare_clickhouse/driver.py
from hare.dialects.registry import DialectRegistry

CLICKHOUSE_DRIVER = ClickHouseDriver()
DialectRegistry.register_driver(CLICKHOUSE_DRIVER)
```

and names that module in the `hare.dialects` entry point group, so hare imports it the first time
it looks up a driver that none of its own has, or lists every driver:

```toml
# pyproject.toml of hare-clickhouse
[project.entry-points."hare.dialects"]
clickhouse = "hare_clickhouse.driver"
```

Once the package is installed, a `"clickhouse://..."` connection URL or a connection config with
`"engine": "clickhouse"` uses it. A second driver or dialect under a taken name or DB_URL scheme
raises `ConfigurationError`. Registering a dialect calls its `install()` once - the place to register
what it adds to hare's own classes (a `QuerySet` method, a path transform on a core field).

## 1. Connecting - the driver {: #driver }

```python
class ClickHouseDriver(Driver):
    name = "clickhouse"                       # the connection config's "engine"
    dialect = CLICKHOUSE_DIALECT
    url_schemes = ("clickhouse",)             # clickhouse://user:password@host:9000/database
    path_credential = "database"              # what the DB_URL path fills
    authority_credentials = {"hostname": "host", "port": "port", "username": "user", "password": "password"}
    default_credentials = {"port": 9000}
    connection_options = ConnectionOptions(   # hare.dialects.base.connection_options
        ConnectionOption("compression", ConnectionOptionType.BOOLEAN),
        ConnectionOption("connect_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=3600),
        ConnectionOption("max_block_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=1_000_000),
    )
    strict_query_parameters = True            # an unknown ?parameter is a ConfigurationError

    def get_client_class(self, credentials):
        return ClickHouseClient

    def get_client_classes(self):
        return (ClickHouseClient,)            # every client class, transaction clients included

    def is_retryable(self, error):
        return False                          # no transactions - nothing to retry as a whole
```

| Member | Meaning |
|---|---|
| `url_schemes`, `path_credential`, `authority_credentials`, `default_credentials`, `url_has_userinfo` | How a DB_URL becomes the client's keyword arguments. |
| `connection_options`, `strict_query_parameters` | The settings a connection takes, each declared once as a `ConnectionOption(name, value_type, *, minimum, maximum, positive, power_of_two, choices)` - its value type is `ConnectionOptionType.WHOLE_NUMBER`, `SECONDS`, `BOOLEAN`, `CHOICE` or `TEXT`. A setting is checked the same way whether it comes typed from a config dict's `credentials` or as text from a DB_URL query parameter (`?connect_timeout=5`): the client calls `options.read(settings)` in its `__init__` to take the known settings out, type- and range-checked, and `options.raise_for_unknown(settings, driver_name)` to reject a misspelled one. No hand-written parsing. |
| `get_client_class(credentials)` | The client class for a connection - it may pick one by a credential and remove it (SQLite's `install_regexp_functions`). |
| `get_client_classes()` | Every client class, transaction clients included. |
| `is_retryable(error)` | Whether a driver error means the database aborted the transaction for a concurrent one (serialization failure, deadlock); hare raises `TransactionRetryError` for it. |
| `get_url_path(url)`, `get_testing_path(path, reuse_databases)` | Reading the DB_URL path, and the path a test run connects to (the `{}` placeholder of `HARE_TEST_DB` filled with a fresh name). |

## 2. Running statements - the client {: #client }

```python
class ClickHouseClient(DatabaseClient):
    driver_name = "clickhouse"
    dialect = CLICKHOUSE_DIALECT
    query_class = ClickHouseQuery
    native_python_types = frozenset({str, int, float, bytes, datetime.datetime, datetime.date, uuid.UUID})
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_returning=False,
        max_bind_parameters=100_000,
    )
```

A client implements `create_connection(with_db)`, `close()`, `db_create()`, `db_delete()`,
`acquire_connection()` (a context manager handing out the raw connection) and the statement
methods - the whole contract between hare and a driver:

| Method | Runs |
|---|---|
| `execute(query, values=None, *, returns_rows=None) -> StatementResult` | One statement. `StatementResult(row_count, rows)` (`hare.dialects.base.results`): the rows it returned - each read by column name, and by position where `Features.supports_positional_rows` - and the rows it changed for a write without `RETURNING`. `returns_rows` says whether the statement returns rows (a `SELECT`, a write with `RETURNING`); `None` leaves the driver to find out from the SQL text. hare passes it for its own reads and for `UPDATE`/`DELETE` statements counting their rows. Every read and write of the ORM goes through it - there is no separate insert or dict-returning method to implement (`execute_dicts()` is built on it; set `row_to_dict` when the driver's rows convert faster than `dict(row)`). |
| `execute_many(query, values)` | One statement once per parameter row; returns nothing. |
| `execute_script(query)` | A script of several statements, passed on verbatim. |
| `execute_described(query, values=None) -> DescribedResult` | One statement with the names of its columns (`columns`, `rows` as tuples, `row_count`) - for showing any result as a table. |
| `copy(table, columns, records, column_types)` | A bulk load, where the database has such a protocol (`Dialect.supports_copy`). |
| `_driver_stream(query, values, chunk_size)` | An async generator over a server-side cursor, where `Features.supports_streaming` - on the transaction client. The public `stream()` is the base class's: it tags, wraps and reports the query around it. |

`native_python_types` is the set of Python types the driver hands back as a field wants them - the
row readers skip conversion for a field of such a type.

Each statement method turns the driver's own exceptions into hare's - `DBConnectionError`,
`IntegrityError`, and `OperationalError` or `TransactionRetryError` through
`_get_operational_error()`, which asks the driver's `is_retryable()` - and reports the call to
[observers and query wrappers](../observability/observers.md). Both happen in one decorator the
client class applies to its statement methods, the way `SqliteClient.translate_exceptions` does:
`DatabaseClient.get_tagged_query_arguments(args, kwargs, method_name)` appends the active query
tags and gives the SQL and parameters; `Observers.run_wrapped(QueryCall(...), proceed)` runs the
call inside the query wrappers when any is installed; `Observers.record_query(sql, params,
start_time, error, connection_name)` reports it once it has finished, successful or not. The
decorator runs on every statement, so it reads the client's `is_transaction_client` (true on
every `TransactionClient`) rather than calling `isinstance()`.

Once a connection opens, the client calls `_post_connect()`, which reads the server's version from
`get_server_version()` - `(major, minor, ...)`, read without query instrumentation; the default
`None` skips the check. A server older than `Dialect.minimum_server_version` is refused with
`UnSupportedError` and the connection closed; `Dialect.get_server_version_features(version)`
returns the `Features` values the version changes (PostgreSQL turns `supports_nulls_distinct` off
before 15), applied to that connection only. A transaction wrapper shares its connection's
`features`.

A database with transactions implements `_get_transaction_client()` - a new client of the driver's
`TransactionClient` subclass, which gives the driver's primitives (see
[Transactions and concurrency](#transactions)). The transaction context around it is hare's own: it
takes the client's resources, begins, and commits or rolls back on exit. The transaction's options
carry the isolation level (`Dialect.get_isolation_level_sql()`), read-only mode and statement
timeout (`_get_transaction_restriction_statements()`).

### Features {: #features }

`Features` (`hare.dialects.base.features`) are what one connection's database and driver support -
fixed once the connection is created, read as `connection.features`:

| Feature | hare's reaction when it's off |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` raise `UnSupportedError`; hare's own multi-statement writes (a cascade, an M2M `add()`, batched `bulk_create()`) run their statements one by one. |
| `can_rollback_ddl` | A migration isn't wrapped in a transaction. |
| `supports_select_for_update` | `select_for_update()` raises `UnSupportedError` when the query runs; `update_or_create()` skips its lock. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` takes a plain `FOR UPDATE` lock. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` of a sliced queryset pick their rows through a `pk IN (SELECT ...)` subquery. |
| `supports_returning` | An `INSERT` asks for nothing back: a key the database generates isn't read onto the instance (give such models a key the application sets), and the values of `db_default` columns are read with a `SELECT` by primary key. An `UPDATE` doesn't read changed generated columns back, and a bulk write can't tell which rows it really inserted. |
| `supports_posix_regex` | A filter with `posix_regex`/`iposix_regex` raises `UnSupportedError` before the query runs. |
| `supports_two_phase_commit` | `Transactions.distributed()` rejects the connection. |
| `supports_listen_notify` | The transactional outbox sends no `NOTIFY` for a new event. |
| `supports_streaming` | `QuerySet.stream()` is unsupported. |
| `inline_comments` | Table and column comments go into `CREATE TABLE` instead of `COMMENT ON`. |
| `supports_positional_rows` | Rows are read by column name only - `select_related()` queries skip their positional fast path. |
| `execute_many_scales_poorly` | Bulk writes are sent as multi-row statements instead of `executemany()`. |
| `max_bind_parameters` | Bulk writes, prefetching and cascades split their statements to stay under it. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` raises `UnSupportedError` before its DDL is sent. |

A client class can derive another's features: `SqliteClient.features.replace(supports_posix_regex=True)`.

### Model-level statements {: #executor }

There is nothing per driver between a model and `execute()`: the same write pipeline builds every
`INSERT`, `UPDATE`, upsert and `DELETE` - `save()`, `bulk_create()`, `QuerySet.update()` alike -
through the dialect's query class, and the same row readers build instances from what `execute()`
returns. What differs between databases is asked from the dialect and the connection's `Features`:
generated and database-default columns come back through `INSERT ... RETURNING` where
`supports_returning`; `Dialect.get_upsert_inserted_flag_sql()` gives the `RETURNING` expression that
tells an inserted row of an upsert from an updated one (PostgreSQL's `xmax = 0`; `None` makes hare
read the existing keys before the write, and only while an observer listens).

## 3. Parameters, names and literals - the dialect {: #dialect }

```python
class ClickHouseDialect(Dialect):
    name = "clickhouse"
    otel_system_name = "clickhouse"           # OpenTelemetry db.system.name
    placeholder_template = "{{p{}}}"         # {p1}, {p2}, ...
    identifier_quote_char = "`"
    alias_quote_char = "`"
    max_identifier_length = None
    supports_schemas = True                   # database.table
```

| Member | Meaning |
|---|---|
| `placeholder_template`, `get_placeholder(index)` | A bound parameter's placeholder, `{}` standing for its index from 1 (`$1` on PostgreSQL, `?` on SQLite). Cached query shapes substitute values by position, so any placeholder style works with the query cache. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)`, `get_concatenated_argument_sql(sql, argument)` | The type a bound literal is cast to where nothing around the parameter types it - by where it stands (`ParameterPosition`: a `CASE` branch, a selected or compared literal, a function argument), as a column's value, as a value of a JSON object, as rendered SQL, or as an argument of a concatenation. None or the SQL unchanged by default; PostgreSQL, which types a parameter only from what's around it, casts. |
| `binds_array_parameters` | Whether a list is bound as one array parameter (a `RawSQL` parameter such as `= ANY(%s)`); without, such a parameter raises `UnSupportedError`. |
| `get_decimal_overflow_predicate_sql(column, max_digits, decimal_places)` | The `WHERE` predicate an `AlterField` finds the stored numbers a `DECIMAL(max_digits, decimal_places)` can't hold with - `CAST(... AS NUMERIC)` by default; a database storing decimals as text overrides it. |
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)` | Quoting of table/column/index/constraint names and of SELECT aliases. |
| `max_identifier_length` | The most bytes a name may take, None for no limit. hare generates names of up to 63 bytes, shortening a longer one with a digest; a dialect with a lower limit can't be registered. |
| `supports_schemas` | Without, a schema-qualified model's table is used unqualified. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_bytes_literal_sql()`, `get_array_literal_sql()`, `get_bindable_number()` | Literals written into SQL text - a string, a column default, bytes, an array - and how a JSON number is bound. Names and literals are escaped only here: the schema editor, the SQL renderer and the introspector all ask the dialect. |
| `get_unbounded_limit_sql()` | The `LIMIT` an `OFFSET` needs without a limit (`LIMIT -1` on SQLite). |
| `get_explain_sql(sql, output_format, options)` | The `EXPLAIN` statement `QuerySet.sql(explain=True)` and `explain()` use. |
| `sql_context` | The context every statement renders in; the query class sets `SQL_CONTEXT = dialect.sql_context`. |

## 4. Column types and values - `build_types()` {: #types }

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
| `to_db`, `to_lookup`, `to_python` | Replace the field's `to_db_value()`, `to_lookup_value()` and `from_db_value()` - each value binds and reads through the query's connection. |
| `json_term` | The text a column's value is written into a JSON object as, for a value stored in a form JSON can't hold (the columnar dialect's 16-byte UUIDs). |

Dialect flags describe how values behave: `enforces_numeric_ranges` (without, hare checks integer
and decimal ranges itself), `supports_virtual_generated_columns`. Where a value is stored or computed
differently, the dialect gives the SQL itself: `get_decimal_compared_term()`, `get_decimal_value_term()`
and `get_decimal_dividend()` for decimals stored as text, `get_json_path_comparand()` for JSON stored as
text, `get_integer_aggregate_as_float()` for an average of integers computed as a decimal,
`get_datetime_part_comparand()` for a date or time of day compared with a datetime. A function the
database has only for some argument types (PostgreSQL's `ROUND(numeric, int)`) is the dialect's
renderer.

A package adding a field can register its storage on any dialect:

```python
DialectRegistry.get_dialect("clickhouse").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## 5. Functions and lookups - `build_renderers()`, `build_filter_operators()` {: #functions-and-lookups }

Every expression renders standard SQL by default. `build_renderers()` returns the `TermRenderers`
that replace what the database writes differently, found through the term's class hierarchy:

```python
def build_renderers(self):
    renderers = TermRenderers()
    renderers.register_function("LENGTH", self.render_length)         # (function, ctx) -> sql
    renderers.register_name(functions.Coalesce, lambda term, ctx: "ifNull")
    return renderers

@staticmethod
def render_length(length, ctx):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], ctx)})"
```

`build_filter_operators()` returns the `FilterOperators` - a mapping from a lookup's own operator to
the dialect's replacement (`FilterOperators({Lookups.is_in: clickhouse_is_in, ...})`, `{}` for
none). An operator only dialects implement (`DialectImplementedOperators`) has to be replaced, or
the lookup is unsupported: `Dialect.supports_lookup()` - and with it `Model._meta.get_lookups(path, dialect)` -
leaves it out, and a filter using it fails before any SQL is built.

A value list too long to bind one parameter per value is the dialect's to shorten: subclass
`LargeInList` (`hare.dialects.base.large_in_list`) and map `Lookups.is_in`/`not_in`/`row_is_in`/
`row_not_in` to its methods of the same names in `build_filter_operators()`. The base class holds
the algorithm once - when the short form applies, how `NULL`s in the list are compared apart, how
`not_in` is negated, the row-value form for a composite key; the dialect gives only the container
the values are bound into as **one** parameter: `get_membership_criterion(field, values,
non_null_values, element_type)` (PostgreSQL's `= ANY($1::type[])`, SQLite's `IN (SELECT value FROM
json_each(?))`) and `get_row_container(value_rows, element_types)` for value rows. Returning `None`
keeps the plain `IN (...)` list.

`is_distinct_from_operator` is the NULL-safe inequality (`IS NOT` on SQLite),
`get_composite_distinct_key()` what `COUNT(DISTINCT ...)` counts a composite key by.

## 6. Statement shapes - the query class {: #query-class }

The query class renders statements through `QueryBuilder`, whose clauses are methods
(`_limit_sql`, `_offset_sql`, `_for_update_sql`, `_returning_sql`, `_on_conflict_sql`,
`_update_sql`, `_delete_sql`, ...). A dialect subclasses both and overrides the clauses its database
writes differently:

```python
class ClickHouseQuery(Query):
    SQL_CONTEXT = CLICKHOUSE_DIALECT.sql_context

    @classmethod
    def _builder(cls, **kwargs):
        return ClickHouseQueryBuilder(**kwargs)


class ClickHouseQueryBuilder(QueryBuilder):
    QUERY_CLS = ClickHouseQuery

    def get_sql(self, ctx=None):
        ...   # ALTER TABLE ... UPDATE / DELETE for updates and deletes
```

Dialect flags cover the rest: `supports_distinct_on`, `sorts_nulls_first`,
`matches_ordering_to_grouping_by_sql`, `supports_conflict_constraint_names`,
`supports_conflict_where`, `guarantees_returning_order`, `supports_copy` with
`supports_copy_column_type()`, and `get_default_rows_source_sql()` for inserting rows of defaults.

### `QuerySet` methods of the dialect {: #queryset-methods }

A database's own query modifiers become `QuerySet` methods without hare knowing them. Register
them in `install()` with a function that gets the query's builder and the call's arguments and
returns the builder - the columnar test dialect's `sample()`:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("sample", self.name, self.apply_sample)

def apply_sample(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).sample(10)` records the call - before `Hare.init()` too - and the query applies
it when it is built for its connection; on a connection of another dialect it raises
`UnSupportedError`. A modifier the database writes in the `FROM` clause (`FINAL`, `SAMPLE 0.1`)
is kept on the dialect's builder by the function and written by its `_from_sql()`. A package can
register a method for a dialect it doesn't define.

A dialect (or a package for one) can add its own QuerySet methods - a columnar database's
`.final()`, `.sample()`, `.prewhere()` - with
`QuerySetExtensions.register(name, dialect_name, apply)` from `hare.query.queryset.extensions`,
usually in its `Dialect.install()`. `apply(builder, *args, **kwargs)` returns the query builder with
the call applied. A call is recorded on the queryset (before `Hare.init()` too) and carried into
`values()`, `count()` and the other queries built from it; when the query is built for its
connection, the implementation of that connection's dialect applies it. On a connection whose
dialect registered none, the query raises `UnSupportedError` instead of dropping the call. A name
QuerySet already has, or a private one, is refused with `ConfigurationError`.

## 7. Transactions and concurrency {: #transactions }

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
| `_is_connection_lost(error)` | Whether a driver error of a `COMMIT`/`ROLLBACK` means the connection is gone - the server rolls such a transaction back. `False` by default. |
| `_is_commit_outcome_unknown(error)` | Whether a lost connection leaves it unknown if a `COMMIT` in flight landed. |
| `_is_commit_rejection(error)` | Whether the database answered the `COMMIT` with an error - the transaction is over. |
| `_is_transaction_finished_error(error)` | Whether the driver reports that an earlier, interrupted `COMMIT`/`ROLLBACK` already ended the transaction. |
| `_check_commit_allowed()`, `_check_savepoint_allowed()`, `_before_top_level_end(event)`, `_after_rejected_commit(error)` | Optional hooks around the state machine's steps - SQLite refuses a `COMMIT` of an aborted transaction and switches a read-only one back before it ends. |
| `_take_transaction_resources()`, `_give_back_transaction_resources()` | Take what a top-level transaction holds for its whole life before `BEGIN` - a pooled connection, the connection's lock - and give it back as soon as the `COMMIT`/`ROLLBACK` landed, before the callbacks run. Nothing by default. |
| `_undo_failed_begin()` | End a transaction whose `BEGIN` raised but may have landed. Nothing by default. |
| `_end_unfinished_transaction()` | End what a top-level transaction left open on the driver once its block exits. Nothing by default. |

The six `_driver_*` methods and `_get_new_savepoint_name()` are abstract; the rest have defaults. A
nested transaction is a client of the same class sharing the connection - the base class makes it.

With transactions, the dialect lists `isolation_levels` weakest first - a transaction asking for a level
runs at the weakest listed one at least as strong (`get_isolation_level()`), and
`get_isolation_level_sql()` sets it. `get_lock_table_sql()` locks a table for the transaction,
`get_migration_lock_sql()` serializes concurrent `migrate` runs, `build_two_phase_commit()` gives
`Transactions.distributed()` its statements.

## 8. DDL - the schema editor {: #schema-editor }

`build_schema_editor_class()` returns a `BaseSchemaEditor` subclass. The base renders standard DDL
from class templates (`TABLE_CREATE_TEMPLATE`, `FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`,
`FK_TEMPLATE`, `ADD_FIELD_TEMPLATE`, `ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); a
dialect overrides the ones its database writes differently and sets a template to `None` where the
statement doesn't exist - the change then rebuilds the table (`_remake_table()`: a new table from
the model, the rows copied, the old one replaced). `_get_table_comment_sql()` and
`_get_column_comment_sql()` write comments.

Dialect flags steer what DDL is written at all:

| Flag | Without it |
|---|---|
| `supports_foreign_keys` | No `FOREIGN KEY` constraints; hare runs every `on_delete` action and `PROTECT` check itself, as for a `db_constraint=False` relation. |
| `supports_unique_constraints` | No unique constraints; a unique `Index` is a plain index; an upsert targets the primary key only. |
| `supports_adding_constraints` | CHECK constraints go into `CREATE TABLE`, unique ones become unique indexes, a later change rebuilds the table. |
| `supports_partial_indexes`, `supports_index_nulls_order`, `supports_concurrent_indexes` | The index part is left out or rejected. |
| `supports_exclusion_constraints`, `supports_deferrable_constraints`, `supports_not_valid_constraints` | The constraint type is rejected. |
| `supports_statement_triggers`, `supports_extensions`, `supports_collations` | `FOR EACH STATEMENT` triggers, `CreateExtension`, `CreateCollation` are rejected or skipped. |
| `truncates_values_on_type_change` | Set it when a narrowing type change silently cuts data - hare checks the data first. |

A flag only says the database has the feature; the base editor writes none of its syntax. The
dialect that sets a flag writes the SQL itself:

| Feature | What the dialect implements |
|---|---|
| Non-key index columns (`include=`) | `Dialect.get_index_include_sql(quoted_columns)` - the clause after the index keys; `""` by default, which leaves the columns out. |
| `UniqueConstraint(nulls_distinct=...)` | `Dialect.get_nulls_distinct_sql(nulls_distinct)` - the clause; also set `Features.supports_nulls_distinct`. |
| `supports_concurrent_indexes` | The editor's `add_index(model, index, concurrently)` and `remove_index(...)`; the base builds and drops the plain way. `_get_index_create_sql()` and `_get_index_drop_sql()` give the plain statements to start from. |
| `supports_not_valid_constraints` | The editor's `add_check_constraint_not_valid()` and `validate_constraint()`; the base adds the constraint as usual and validates nothing. |
| `supports_exclusion_constraints` | The editor's `_exclusion_constraint_sql(model, constraint)` - the constraint's definition. |
| `supports_extensions` | The editor's `create_extension()`, `drop_extension()` and `_get_extension_create_sql()`. |
| `supports_collations` | The editor's `create_collation()` and `drop_collation()`. |
| `supports_schemas` | The editor's `move_table_to_schema()`, for a model whose `Meta.schema` changes. |

The same goes for lookups only one database has: the dialect registers them from its `install()`
with [`Field.register_lookup()`](custom-lookups.md), naming
itself in `dialects=` and the extension they need in `required_extension=` - as PostgreSQL does
for its trigram lookups.

### Table options {: #table-options }

A database whose `CREATE TABLE` takes more than columns declares a `TableOptions` subclass; models
list it in `Meta.table_options`, one entry per dialect, and a connection uses its own dialect's
entry:

```python
@dataclasses.dataclass(frozen=True)
class ClickHouseTableOptions(TableOptions):
    dialect_name: ClassVar[str] = "clickhouse"

    engine: str = "MergeTree()"
    order_by: tuple[str, ...] = ()
    partition_by: str | None = None

    def raise_if_unsupported(self, model):
        if not self.order_by:
            raise ConfigurationError(f"{model.__name__}: ClickHouseTableOptions needs order_by")

    def get_create_suffix_sql(self, model, quote):
        sql = f" ENGINE = {self.engine} ORDER BY ({', '.join(quote(column) for column in self.order_by)})"
        return sql + (f" PARTITION BY {self.partition_by}" if self.partition_by else "")


class Event(Model):
    class Meta:
        primary_key = None
        table_options = [ClickHouseTableOptions(order_by=("created_at",))]
```

The dialect names the class in `build_table_options_class()`. Migrations keep the options in the
model state and write them into migration files through `deconstruct()`; a change of them is applied
by the schema editor's `alter_table_options()` - by default the table is rebuilt with the new
options. `hare drift` compares them with what the introspector reads, and `inspectdb` writes them
into `Meta.table_options` (see the introspector below).

Options that hold parts of the table the migrations add and remove one at a time - partitions -
take four more hooks, all with a default that means "none":

| Hook | What it gives |
|---|---|
| `get_partitions()` | The partitions by name; each partition object names its dialect as `dialect_name` and has a `name` and a `deconstruct()`. |
| `with_partitions(partitions)` | The options with another set of them. |
| `can_change_partitions_to(new_options)` | Whether the other options are reached by adding and removing partitions - else the change is one `AlterModelOptions`. |
| `with_field_names(column_to_field_name)` | The options naming model fields where they name columns - options read from a database name columns. |

`makemigrations` then writes `AddPartition`/`RemovePartition` for each partition that came or went,
and they call the schema editor's `add_partition(model, partition)`/`remove_partition(model,
partition)` (`UnSupportedError` by default); `_get_partition_create_sqls(model, safe)` returns the
statements creating the partitions right after the table's `CREATE TABLE`. PostgreSQL's
[partitioned tables](../models/meta-options.md#partitioning) are built on these hooks.

### Models without a primary key {: #models-without-a-primary-key }

A table of a columnar database often has no primary key: `Meta.primary_key = None` (see
[Meta options](../models/meta-options.md#primary_key)). Reads, filters, aggregates,
`bulk_create()` and `QuerySet.update()`/`delete()` work; what needs a key to identify a row
(`save()` of a fetched row, `instance.delete()`, relations to the model, ...) raises
`ConfigurationError`.

## 9. Reading an existing schema - the introspector {: #introspector }

`build_introspector_class()` returns a `SchemaIntrospector` subclass implementing
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` and
`fetch_tables(connection, tables, schema) -> list[TableInfo]`. The rest - `inspectdb`, drift, the
reverse type mapping to fields - works from the neutral `TableInfo`/`ColumnInfo`/`IndexInfo`/
`ForeignKeyInfo`. A table's storage goes into `TableInfo.table_options` through
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, which keeps the names the
class declares and gives None when every option has its default; `get_declared_table_options()`
lets drift take a declaration the database records another way as the same storage. Without an introspector (`None`, the default) `inspectdb` and `hare drift` raise
`UnsupportedDialectError`.

## 10. Migrations {: #migrations }

The migration journal and every operation go through the schema editor, so they need nothing of
their own. `Features.can_rollback_ddl` decides whether a migration runs in a transaction,
`get_migration_lock_sql()` whether two `migrate` runs wait for each other, and
`get_connection_only_function(sql)` names a function hare installs on its own connections that
DDL can't use (the SQLite functions hare registers on each connection).

## 11. What hare does for a database without guarantees {: #without-guarantees }

| The database lacks | hare |
|---|---|
| Transactions | Rejects `atomic()`; runs its own multi-statement writes statement by statement. |
| Foreign keys | Runs `on_delete` and `PROTECT` in Python. |
| Unique constraints | Creates none; an upsert conflicts on the primary key only. |
| `RETURNING` | An `UPDATE` doesn't read changed generated columns back; a key the database generates isn't read onto the instance; `db_default` values are read with a `SELECT` after the `INSERT`. |
| Deferrable constraints | `defer_cascade_foreign_keys()` raises `UnSupportedError` - a hard delete through a `PROTECT` guard in the same cascade can't be deferred. |
| A primary key | Supports keyless models as above. |

`checks_foreign_keys_per_cascade_step` and `checks_restrict_at_statement_end` of the dialect and
`cascade_depth_limit` of the client's `Features` describe how a native cascade behaves, so hare
knows when to defer or run it itself. A client whose features set `cascade_depth_limit` raises
`CascadeDepthLimitError` from `hare.exceptions` - or a subclass of its own, as SQLite's
`SqliteTriggerRecursionLimitError` - when the database stops a cascade at that depth; `delete()`
catches it, rolls the attempt back and walks the cascade in Python.

## 12. Testing {: #testing }

hare's own suite runs against a dialect: install the package (its entry point registers the
driver) and point `HARE_TEST_DB` at it (`clickhouse://.../test_{}`, the `{}` filled per run) - every
test the dialect's features allow runs. A test of something a database may lack is marked with the
features it needs:

```python
from hare.contrib.test import requires_features

@requires_features(supports_transactions=True)
async def test_rollback(db): ...

@requires_features(supports_foreign_keys=True)      # a Dialect flag works as well
async def test_cascade_in_the_database(db): ...
```

`tests/test_dialect_contract.py` checks every registered dialect and driver: names, schemes,
features, the registries, the schema editor, the introspector and deterministic SQL. A test
database is reset between tests with `Dialect.clear_tables()` (by default one script of a `DELETE FROM`
per table).

## 13. Observability and speed {: #observability }

`otel_system_name` is the OpenTelemetry `db.system.name` value (`clickhouse`, or `other_sql`). The Rust
row readers depend on the client's `native_python_types`, not on the dialect's name. Every statement
the client runs reaches [`Observers`](../observability/observers.md) - `QueryExecuted`, query
wrappers, slow-query logging - through the decorator described in [the client](#client); a client
that skips it is invisible to `capture_queries`, the N+1 detector and OpenTelemetry.

## Checklist {: #checklist }

- [ ] `Dialect` subclass: name, `otel_system_name`, flags, `build_types()`,
      `build_filter_operators()`, `build_renderers()`, `get_explain_sql()`.
- [ ] Query class and builder with `SQL_CONTEXT = dialect.sql_context`.
- [ ] Client: statements, error translation, `features`; transactions where the database has them.
- [ ] Driver: name, schemes, credentials, client classes; registered on import.
- [ ] `hare.dialects` entry point.
- [ ] Schema editor templates, table options, introspector - as the database needs.
- [ ] hare's suite green under `HARE_TEST_DB`, including `tests/test_dialect_contract.py`.
