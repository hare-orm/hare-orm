# Dialects & features

A **dialect** is the SQL language, column types, DDL and catalog of one type of database; a
**driver** is how hare connects to it. One dialect can have several drivers - PostgreSQL has two.
A connection's `engine` (or its DB_URL scheme - the same word) picks the driver, and with it the
dialect; see [Choosing a PostgreSQL driver](../connections/connections.md#choosing-a-postgres-driver).

| Dialect | `engine` / DB_URL scheme | Driver | Oldest server |
|---|---|---|---|
| `sqlite` | `sqlite` | `aiosqlite` over the standard `sqlite3` module | SQLite 3.35.0 |
| `postgresql` | `postgresql` | hare's Rust driver | PostgreSQL 14 |
| `postgresql` | `postgresql+asyncpg` | `asyncpg` (`hare-orm[asyncpg]`) | PostgreSQL 14 |

`aiosqlite` runs each SQLite connection on a thread of its own, and that thread doesn't keep the
interpreter alive: a program that ends with a connection still open - on an exception, say -
exits instead of hanging, and SQLite's journal rolls an unfinished transaction back.

Everything hare does differently per database - SQL syntax, column types, how a value is written
and read, DDL, reading an existing schema - is decided by the connection's dialect and its
`Features`, never by comparing database names. A query runs in the dialect of the connection it
runs on (`using()`, a transaction, a router or the model's default connection), so one model
can be read from SQLite and PostgreSQL in the same process.

## The server version {: #the-server-version }

Right after a connection opens, hare reads the server's version - without an observer seeing it
(the PostgreSQL drivers read `server_version_num`, SQLite reports its library version). A server
older than its dialect's `minimum_server_version` is refused before anything else runs, and the
connection is closed:

```text
UnSupportedError: The postgresql server is version 13.9, older than 14, the oldest hare runs on
```

A feature that exists only from some version on follows the server's own version: a PostgreSQL 14
connection has `features.supports_nulls_distinct` off, so a
`UniqueConstraint(..., nulls_distinct=False)` on it raises `UnSupportedError` before any DDL is
sent. `Hare.generate_schemas()` connects before it writes the DDL for the same reason.

## `connection.dialect` and `connection.features` {: #connection-dialect-and-features }

```python
connection = Book.get_connection()
connection.dialect.name                 # "postgresql"
connection.dialect.supports_distinct_on # True
connection.features.supports_returning  # True
```

`connection.dialect` is the dialect object, one per dialect, shared by every connection that
speaks it - what the database's SQL can express. `connection.features` is a frozen `Features` of
this connection - what its server version and driver support, fixed once it has connected.

### `Features` {: #features }

| Field | Meaning | `sqlite` | `postgresql` | `postgresql+asyncpg` |
|---|---|---|---|---|
| `supports_transactions` | Transactions and savepoints | yes | yes | yes |
| `can_rollback_ddl` | DDL runs inside a transaction and rolls back with it | yes | yes | yes |
| `supports_select_for_update` | `SELECT ... FOR UPDATE` | no | yes | yes |
| `supports_select_for_no_key_update` | `SELECT ... FOR NO KEY UPDATE` | no | yes | yes |
| `supports_update_limit_order_by` | `UPDATE`/`DELETE` with `ORDER BY` and `LIMIT` | no | no | no |
| `supports_posix_regex` | POSIX regular expression lookups | with `install_regexp_functions=True` | yes | yes |
| `supports_returning` | `INSERT ... RETURNING` | yes | yes | yes |
| `supports_two_phase_commit` | `PREPARE TRANSACTION` / `COMMIT PREPARED` | no | yes | yes |
| `supports_listen_notify` | `LISTEN` / `NOTIFY` | no | yes | yes |
| `inline_comments` | Table and column comments go inside `CREATE TABLE` | yes | no | no |
| `supports_positional_rows` | Result rows read by position as well as by column name | yes | yes | yes |
| `supports_streaming` | `QuerySet.stream()` pages rows off a server-side cursor | no | yes | yes |
| `execute_many_scales_poorly` | `executemany()` is slower than one multi-row statement | no | yes | no |
| `max_bind_parameters` | Most bind parameters one statement may carry | 32766 | 32767 | 32767 |
| `cascade_depth_limit` | The recursion depth a native `ON DELETE CASCADE` stops at; the client then raises `CascadeDepthLimitError`, and hare finishes a deep cascade itself, fewer levels than this at a time | the build's `SQLITE_LIMIT_TRIGGER_DEPTH` (1000; 100 in the official 3.37.2 library) | none | none |
| `supports_nulls_distinct` | A unique constraint takes `NULLS [NOT] DISTINCT` | no | from 15 | from 15 |
| `supports_partitioned_exclusion_constraints` | A partitioned table takes an exclusion constraint | no | from 17 | from 17 |

Where a feature is missing, the operation that needs it raises `UnSupportedError` naming it before
anything is sent - `select_for_update()` on SQLite, `Transactions.distributed()` without two-phase
commit, `stream()` without streaming.

### Dialect capabilities {: #dialect-capabilities }

The attributes of `connection.dialect` that differ between the built-in dialects:

| Attribute | Meaning | `sqlite` | `postgresql` |
|---|---|---|---|
| `supports_schemas` | A table can be qualified by a schema (`Meta.schema`); without, the schema is ignored | no | yes |
| `supports_distinct_on` | `SELECT DISTINCT ON (...)` - `distinct("field")` | no | yes |
| `sorts_nulls_first` | NULL sorts before every value in ascending order by default | yes | no |
| `enforces_numeric_ranges` | Integer and decimal columns reject out-of-range values themselves; without, hare checks them | no | yes |
| `guarantees_returning_order` | A multi-row `INSERT ... RETURNING` returns rows in insert order - `bulk_create(returning=True)` | no | yes |
| `supports_conflict_constraint_names` | `bulk_create(on_conflict_constraint=...)` | no | yes |
| `supports_conflict_where` | `bulk_create(conflict_where=...)` | no | yes |
| `supports_copy` | The bulk `COPY` protocol - `bulk_create(use_copy=True)` | no | yes |
| `supports_virtual_generated_columns` | A generated column can be computed on read (`stored=False`) | yes | no |
| `isolation_levels` | The isolation levels a transaction runs at - see [Isolation level](../connections/transactions.md#isolation-level) | `SERIALIZABLE` | all four |
| `max_identifier_length` | The most bytes a name may take. hare keeps every name it generates within 63 bytes, shortening a longer one with a hash, and refuses to register a dialect with a lower limit | none | 63 |
| `supports_adding_constraints` | `ALTER TABLE ... ADD CONSTRAINT`; without, a later constraint change rebuilds the table | no | yes |
| `supports_partial_indexes` | An index takes a `WHERE` condition | yes | yes |
| `supports_exclusion_constraints` | `ExclusionConstraint` | no | yes |
| `supports_deferrable_constraints` | `deferrable=True` on a constraint or trigger | no | yes |
| `supports_index_nulls_order` | An index key sets where NULLs sort | no | yes |
| `supports_concurrent_indexes` | `CONCURRENTLY` index operations | no | yes |
| `supports_not_valid_constraints` | A constraint added `NOT VALID` and validated later | no | yes |
| `supports_statement_triggers` | `TriggerForEach.STATEMENT` | no | yes |
| `supports_extensions` | `CREATE EXTENSION` | no | yes |
| `supports_collations` | `CREATE COLLATION` | no | yes |
| `supports_foreign_keys` | Foreign keys are enforced; without, hare runs every `on_delete` action itself | yes | yes |
| `supports_unique_constraints` | Uniqueness is enforced | yes | yes |
| `minimum_server_version` | The oldest server hare runs on | 3.35.0 | 14 |

Each attribute is documented on `hare.dialects.base.dialect.Dialect`. How a dialect stores a
table - SQLite's `WITHOUT ROWID`, PostgreSQL's tablespace, `UNLOGGED`, storage parameters and
[partitioning](../models/meta-options.md#partitioning) (hash, list and range partitions, added and
removed by migrations) - is set per model with
[`Meta.table_options`](../models/meta-options.md#table_options).

## `DialectRegistry` {: #dialectregistry }

```python
from hare.dialects.registry import DialectRegistry

DialectRegistry.get_dialect("postgresql")                  # the dialect object
DialectRegistry.get_driver("postgresql+asyncpg")           # the driver an engine names
DialectRegistry.get_driver_for_url_scheme("sqlite")        # the driver a DB_URL scheme picks
DialectRegistry.get_dialects(), DialectRegistry.get_drivers()
```

An unknown name or scheme raises `ConfigurationError` listing the registered ones. A built-in
dialect or driver is imported and registered only when a connection or a lookup names it, so
`Hare.init()` on `sqlite://` imports nothing of PostgreSQL and `Hare.init()` on `postgresql://`
nothing of SQLite; `get_dialects()` lists the built-in dialects first, in the same order every
time. A name or scheme
no built-in driver has, and a call to `get_dialects()`/`get_drivers()`, loads every module named in the
`hare.dialects` entry point group of an installed package - a third-party dialect registers its
drivers on import:

```toml
[project.entry-points."hare.dialects"]
clickhouse = "hare_clickhouse.driver"
```

Its engine and DB_URL scheme then work like the built-in ones. How to write one is in
[Writing a dialect](../extending/writing-a-dialect.md).

### Registering after `Hare.init()` {: #registering-after-init }

A dialect, a driver, a term renderer or type mapping of a dialect, a `register_lookup()` lookup, a
path transform and a dialect's QuerySet method can be registered at any time. Every registration
drops what hare cached from the registries - the SQL of queries, row-reading plans, filter and
ordering descriptions - and rebuilds the filters of every bound model, so the next query uses it;
a query already built runs on with the SQL it has. Registrations usually run on import, before
`Hare.init()`, when nothing is cached yet and the drop costs nothing.

## Tests by capability {: #tests-by-capability }

[`requires_features()`](../testing.md#requires-features) skips a test
unless the connection has what it needs - a field of `Features`, a dialect attribute, or the
dialect's name:

```python
@requires_features(supports_distinct_on=True)
async def test_latest_per_author(db): ...
```
