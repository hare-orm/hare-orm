# SQLite: what runs differently

SQLite is a library writing one file: it has no server, lets one connection write at a time, keeps no
exact decimal or date types and changes a table's columns mostly by building the table anew. Each row
below says what hare does about a part of SQL SQLite lacks or treats in its own way. What SQLite has none
of at all — `MERGE`, `LATERAL`, sequences, row level security and the rest — is in the
[features table](../dialects-and-features.md#database-features).

## <a id="connections-and-transactions"></a>Connections and transactions

| SQLite | hare |
|---|---|
| One writer at a time | hare keeps one connection per connection alias, taken like a pool's: a transaction holds it to its end, and a query of another task on the same alias waits for it — see [Pool status](../../observability/pool-health.md#pool-status). Another connection to the file waits for SQLite's lock up to `busy_timeout`. |
| Isolation levels | Every transaction is `SERIALIZABLE`; `atomic()` accepts any level — see [Isolation level](../../connections/transactions.md#isolation-level). |
| Read-only transactions | `PRAGMA query_only` is on for the transaction — see [Read-only transactions](../../connections/transactions.md#read-only-and-statement-timeouts). |
| A transaction aborted by an error | A failed statement leaves the transaction usable — see [A failed statement in a transaction](../../connections/transactions.md#failed-statement). An interrupted write rolls back the whole transaction, savepoints included, and hare then treats the transaction as aborted — see [Read-only transactions and statement timeouts](../../connections/transactions.md#read-only-and-statement-timeouts). |
| Row locks (`SELECT ... FOR UPDATE`) | `select_for_update()` raises `UnSupportedError` when the query runs — see [`select_for_update()`](../../querying/queryset-methods.md#select-for-update). |
| A database shared by connections in memory | Every `:memory:` connection is a database of its own: `autonomous()` and a second connection alias see none of its tables. |

## <a id="values"></a>Values

| SQLite | hare |
|---|---|
| An exact decimal type | A `DecimalField` is stored as text and compared by its exact value — see [Decimals](functions.md#decimals). |
| Date and time types | Dates, times and datetimes are stored as ISO text; hare's functions truncate, extract and shift them — see [Functions](functions.md#registered-functions). |
| Integer ranges of a column type | Any 64-bit integer fits any integer column, so hare checks a written value against its field's range, an `update()` from an expression included, and refuses an out-of-range integer result of arithmetic (`integer out of range`). |
| A negative zero | A `FloatField`'s `-0.0` reads back as `0.0`. |
| `NULL` placement in an ordering | `NULL` sorts as the smallest value, first ascending — see [NULL ordering](../../querying/queryset-methods.md#null-ordering). |
| Text order of a locale | Text compares by its code points. |
| `jsonb` | A `JSONField` is `JSON_TEXT`; hare's functions compare, order and print it as `jsonb` — see [JSON](functions.md#json). |

## <a id="queries"></a>Queries

| SQLite | hare |
|---|---|
| Rows of a multi-row `RETURNING` in insert order | `bulk_create(returning=True)` needs every object's pk set and matches the rows back by it — see [`bulk_create()`](../../querying/queryset-methods.md#bulk-create). |
| `DISTINCT ON` | `distinct(*fields)` picks the same rows by row number — see [`distinct()`](../../querying/queryset-methods.md#distinct). |
| Grouping sets | `Rollup`, `Cube`, `GroupingSets` and `Grouping()` raise `UnSupportedError` — see [Several groupings](../../querying/aggregation.md#grouping-sets). |
| POSIX regular expressions | `__posix_regex` and `__iposix_regex` need `install_regexp_functions=true` — see [Regular expressions](functions.md#regular-expressions). |
| More than 64 tables in one query | The query fails with an `OperationalError` saying so; shorten the relation paths. |
| Long `__in` lists as bound parameters | From 20 values, the list is bound as one JSON array read by `json_each()` — see [Large `__in` lists](../../querying/filters.md#large-in-lists). |

## <a id="schema"></a>Schema and migrations

| SQLite | hare |
|---|---|
| `ALTER TABLE` for most column and constraint changes | The table is rebuilt: a copy made from the model, filled from the old table and renamed into its place — see [Rebuilding a table (SQLite)](../../migrations/migrations.md#sqlite-table-rebuild). |
| Foreign keys checked during a rebuild | Every migration runs with `PRAGMA foreign_keys = OFF` and checks the rows with `PRAGMA foreign_key_check` — see [Foreign keys during a migration (SQLite)](../../migrations/migrations.md#sqlite-foreign-key-check). |
| Schemas | `Meta.schema` is ignored: the table is created and queried unqualified. A schema per tenant and row level security raise `UnSupportedError` — see [Schema per tenant](../../soft-delete-versions-tenants/schema-per-tenant.md). |
| Statement triggers, deferrable triggers and constraints, a disabled trigger | `TriggerForEach.STATEMENT`, `deferrable=True` and `enabled=False` raise `UnSupportedError` — see [Triggers](../../models/constraints-and-triggers.md#triggers). |
| `NULLS NOT DISTINCT` | `UniqueConstraint(nulls_distinct=False)` raises `ConfigurationError` when the model is bound. |
| `INCLUDE` columns, `NULL` placement of an index key | An index is created without its `include` columns; a key setting where `NULL`s sort raises `UnSupportedError` — see [Indexes](../../models/indexes.md). |
| `LIKE` in a constraint or an index condition | A text lookup is written as `GLOB`, matching the same rows case-sensitively — see [Constraints](../../models/constraints-and-triggers.md#constraints). |
| hare's functions outside hare | A constraint or an index needing one raises `UnSupportedError` — see [In constraints and indexes](functions.md#in-constraints-and-indexes). |
| Collation and extension DDL | `CreateCollation`/`RemoveCollation` and `CreateExtension`/`RemoveExtension` are skipped with a warning — see [Collations](../../migrations/operations.md#raw-operations). |

## <a id="search-and-geodata"></a>Search and geodata

| SQLite | hare |
|---|---|
| Text search configurations | `FullTextIndex` keeps an FTS5 table in step by triggers — see [Full-text search](../search-and-geodata/full-text-search.md). |
| Vector types | sqlite-vec, with `load_sqlite_vec=true` — see [Vector search](../search-and-geodata/vector-search.md). |
| Spatial types | SpatiaLite, with `load_spatialite=true` — see [GIS](../search-and-geodata/gis.md). |
