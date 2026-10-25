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
| An exact decimal type | A `DecimalField` has no column on SQLite, and a `Decimal` value in a query run on SQLite is refused before any SQL — see [`DecimalField` (SQLite)](../../models/field-types.md#decimal-field-sqlite). |
| Date and time types | Dates, times and datetimes are stored as ISO text; hare's functions truncate, extract and shift them — see [Functions](functions.md#registered-functions). |
| Integer ranges of a column type | Any 64-bit integer fits any integer column, so hare checks a value it writes against its field's range. A value the database computes is SQLite's own — see [Results](#results). |
| A negative zero | A `FloatField`'s `-0.0` reads back as `0.0`. |
| A `NaN` | A `FloatField` refuses `NaN`: SQLite stores it as `NULL`. |
| A null byte in a `LIKE` pattern or a full-text query | The pattern is refused: `LIKE` reads it up to the null byte and would match what the pattern doesn't hold (`Features.cuts_like_pattern_at_null_byte`); so is the text of a `SearchQuery` — of `__search`, `SearchRank` and `SearchHeadline` alike — which FTS5 reads the same way. A null byte in a stored text or JSON value is kept. |
| A long `LIKE` pattern | SQLite refuses a pattern past 50000 bytes (`SQLITE_MAX_LIKE_PATTERN_LENGTH`) — `__contains` and the other text lookups of a longer text raise `ValidationError`. |
| `NULL` placement in an ordering | `NULL` sorts as the smallest value, first ascending — see [NULL ordering](../../querying/queryset-methods.md#null-ordering). |
| Text order of a locale | Text compares by its code points. |
| `jsonb` | A `JSONField` is `JSON_TEXT`; `contains` and `contained_by` raise `UnSupportedError` — SQLite's JSON has no containment. hare adds the meaning of its paths with a digit segment and date comparison, and SQLite's `json_extract()` orders and compares the rest — see [JSON](functions.md#json). |
| A null byte in the text of raw SQL | The text of `execute_sql()`, `RawSQL` and the rest of raw SQL is sent as written; SQLite's driver refuses a null byte in it (`the query contains a null character`). |

## <a id="results"></a>Results that are SQLite's own

hare doesn't make SQLite give PostgreSQL's result. It checks what it guarantees itself — a value it
writes, an enum's member — writes the forms it stores itself (a date as text, a JSON path) and
supplies a function SQLite lacks; everything else is
SQLite's own, and differs from PostgreSQL's:

- **JSON.** Ordering and comparison of a path or of a whole `JSONField` read the value `json_extract()`
  gives (a string as itself, `true` and `false` as 1 and 0, a JSON `null` as `NULL`, an object or an array as
  its minified text; numbers before text). Equality of a path compares the JSON form: `true` and `1`
  differ, a string and a number differ. A whole `JSONField` is equal when its stored text is, so key order
  and `1` against `1.0` matter. `has_key` finds a key of an object only; SQLite before 3.41 refuses the path
of an empty key (`has_key=""` — *JSON path error*). A value read as text, a `Cast()`
  to text and a `Concat()` of it are the stored text without spaces. See [JSON](functions.md#json).
- **Integer arithmetic** is SQLite's own: `update(n=F("n") + 1)` keeps a number past the field's range,
  and an integer result past 64 bits is a `REAL`. An integer field set from a fraction
  (`update(n=F("n") * 2.5)`) takes the integer SQLite's `CAST` gives, the fraction cut off — an `INTEGER`
  column would keep the fraction.
- **`Cast()`** to a number or a boolean is SQLite's own `CAST`: to an integer it cuts the fraction off,
  where PostgreSQL rounds; text that isn't a number is `0` (`' 42 '` is `42`); a boolean is the value read
  as a number, true when it isn't `0` (`'true'` is false). A cast to text is SQLite's own: a
  float `2.0` with 15 significant digits (17 from SQLite 3.53), a boolean `1` or `0`. See
  [Casts](functions.md#casts).
- **The text of a value.** `Concat()` of a column joins SQLite's text of it (a boolean `1` or `0`, a float
  `-3.0`); a Python literal is its Python text (a boolean `true` or `false`, a float by `repr()`). A text
  lookup on a boolean or float column matches SQLite's text: `1`, `0`, `2.0`. A naive datetime
  (`use_timezone=False`) is written at the machine's local zone.
- **Regular expressions** are Python's `re`: `__iposix_regex` is `re.IGNORECASE`, PostgreSQL's
  `[[:digit:]]`, `\y` and `(?n)` aren't translated, and an invalid pattern raises with Python's message.
- **Case mapping.** `Upper()`, `Lower()` and the case-insensitive lookups are SQLite's `UPPER()`,
  `LOWER()` and `LIKE`, which fold the ASCII letters only: `Upper("café")` is `CAFé`, and `iexact="ПРИВЕТ"`
  doesn't match `привет` — see [Case mapping](functions.md#case-mapping).
- **Times and addresses.** A `TimeField` and an `IPAddressField` order and compare as their stored text:
  `10:00:00+03:00` sorts after `08:00:00+00:00`, though it is the earlier moment, and `10.0.0.2` after
  `10.0.0.10`. To order moments, keep them in a `DatetimeField`.
- **Numbers.** A division or a `MOD` by zero gives `NULL`, and so does a math function outside its domain
  (`Ln(0)`, `Sqrt(-1)`); PostgreSQL raises. `Round()` of a float is SQLite's `ROUND()`. `Greatest()` and
  `Least()` give `NULL` when an argument is `NULL`; PostgreSQL skips the `NULL`s.
- **Text functions.** `Substr`, `Chr` and `Ord` are SQLite's `substr()`, `char()` and `unicode()`. `Left`,
  `Right`, `LPad`, `RPad`, `Repeat`, `Reverse` and the digests are hare's, with no 32-bit limit on a length,
  a position or a count; a result longer than SQLite holds in one value (1e9 bytes) is refused. A text
  function given an expression of the wrong type is left to SQLite.
- **`JSONObject()` and `JSONArray()`** are written by `json_object()`: a boolean as 1 or 0, a float with 15
  significant digits (17 from SQLite 3.53), bytes as uppercase hex.

## <a id="queries"></a>Queries

| SQLite | hare |
|---|---|
| Rows of a multi-row `RETURNING` in insert order | `bulk_create(returning=True)` needs every object's pk set and matches the rows back by it — see [`bulk_create()`](../../querying/queryset-methods.md#bulk-create). |
| `DISTINCT ON` | `distinct(*fields)` raises `UnSupportedError` — see [`distinct()`](../../querying/queryset-methods.md#distinct). |
| Grouping sets | `Rollup`, `Cube`, `GroupingSets` and `Grouping()` raise `UnSupportedError` — see [Several groupings](../../querying/aggregation.md#grouping-sets). |
| POSIX regular expressions | `__posix_regex` and `__iposix_regex` need `install_regexp_functions=true` and are matched by Python's `re`, its syntax and semantics — see [Regular expressions](functions.md#regular-expressions). |
| More than 64 tables in one query | The query fails with an `OperationalError` saying so; shorten the relation paths. |
| Long `__in` lists as bound parameters | From 20 values, the list is bound as one JSON array read by `json_each()` — see [Large `__in` lists](../../querying/filters.md#large-in-lists). |

## <a id="schema"></a>Schema and migrations

| SQLite | hare |
|---|---|
| `ALTER TABLE` for most column and constraint changes | The table is rebuilt: a copy made from the model, filled from the old table and renamed into its place — see [Rebuilding a table (SQLite)](../../migrations/migrations.md#sqlite-table-rebuild). |
| Foreign keys checked during a rebuild | Every migration runs with `PRAGMA foreign_keys = OFF` and checks the rows with `PRAGMA foreign_key_check` — see [Foreign keys during a migration (SQLite)](../../migrations/migrations.md#sqlite-foreign-key-check). |
| Schemas | `Meta.schema` names an attached database the connection attaches (`attached_databases`); migrations create none — see [Attached databases](connecting.md#attached-databases). A schema per tenant and row level security raise `UnSupportedError` — see [Schema per tenant](../../soft-delete-versions-tenants/schema-per-tenant.md). |
| Statement triggers, deferrable triggers and constraints, a disabled trigger | `TriggerForEach.STATEMENT`, `deferrable=True` and `enabled=False` raise `UnSupportedError` — see [Triggers](../../models/constraints-and-triggers.md#triggers). |
| `NULLS NOT DISTINCT` | `UniqueConstraint(nulls_distinct=False)` raises `ConfigurationError` when the model is bound. |
| `INCLUDE` columns, `NULL` placement of an index key | An index is created without its `include` columns; a key setting where `NULL`s sort raises `UnSupportedError` — see [Indexes](../../models/indexes.md). |
| `LIKE` in a constraint or an index condition | A text lookup is written as `GLOB`, matching the same rows case-sensitively — see [Constraints](../../models/constraints-and-triggers.md#constraints). |
| hare's functions outside hare | A constraint or an index needing one raises `UnSupportedError` — see [In constraints and indexes](functions.md#in-constraints-and-indexes). |
| Collation and extension DDL | `CreateCollation`/`RemoveCollation` and `CreateExtension`/`RemoveExtension` are skipped with a warning — see [Collations](../../migrations/operations.md#raw-operations). |

`AlterField` to another type refuses only the stored values the new field can't read, before the rebuild
(`the column holds "abc", which is no integer`). Numbers and booleans convert into each other (a non-zero
number is true), a number made an integer loses its fraction, a boolean made text is SQLite's own text
(`1` or `0`), and a text made a boolean reads `true`, `false`, `1` or `0`.

## <a id="search-and-geodata"></a>Search and geodata

| SQLite | hare |
|---|---|
| Text search configurations | `FullTextIndex` keeps an FTS5 table in step by triggers; `SearchRank` and `SearchHeadline` are FTS5's `bm25()`, `highlight()` and `snippet()` — `NULL` for a row the query doesn't match, and a `SearchHeadline` needs its `start_sel` and `stop_sel` — see [Full-text search](../search-and-geodata/full-text-search.md). |
| Vector types | sqlite-vec, with `load_sqlite_vec=true` — see [Vector search](../search-and-geodata/vector-search.md). |
| Spatial types | SpatiaLite, with `load_spatialite=true` — see [GIS](../search-and-geodata/gis.md). |
