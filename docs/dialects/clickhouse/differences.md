# ClickHouse: what runs differently

ClickHouse lacks parts of SQL hare would otherwise rely on: unique and foreign key constraints, an
`UPDATE` in place, `RETURNING`, `ON CONFLICT`, row locks. Transactions and generated keys exist only on a
server with a ClickHouse Keeper. Each row below says what hare does instead.

| ClickHouse lacks | hare |
|---|---|
| Transactions, by default | `atomic()` raises `UnSupportedError` unless the connection asks for them with `transactions=true` — see [Transactions and locks](transactions-and-locks.md). Without, hare's own multi-statement writes run one statement after another, a failure leaving the statements before it written. |
| Savepoints | A nested `atomic()` joins the transaction it is nested in; an error leaving it rolls the whole transaction back. |
| Generated keys | On ClickHouse 25.1 and later with a Keeper, the keys are taken from a series before the rows are written (`generateSerialID`) — see [Models](models.md#keys). Elsewhere, a model whose key the database generates is refused when it is bound. |
| Unique and foreign key constraints | hare checks the uniqueness and the relations a model declares before it writes — see [Models](models.md#uniqueness-and-relations). |
| An `UPDATE` of rows in place | `save()`, `QuerySet.update()` and `bulk_update()` run an `ALTER TABLE ... UPDATE` mutation, finished before the call returns — see [An `UPDATE` of rows in place](#an-update-of-rows) |
| A row count of an UPDATE or DELETE | The rows a mutation or a DELETE matches are counted right before it — another writer changing them in between changes the count. |
| `RETURNING` | The rows are read by their keys: an update's after it, a delete's before it, the values the server gave inserted rows (`db_default`, `MATERIALIZED` columns) after the insert — see [`RETURNING`](#returning) |
| `ON CONFLICT` | `bulk_create(ignore_conflicts=True)` and `bulk_create(update_fields=[...], on_conflict=[...])` read the keys of the batch's conflict fields first: the rows already stored are skipped or updated with `bulk_update()`, the rest inserted — see [`ON CONFLICT`](#on-conflict) |
| Batches of an UPDATE | `BackfillColumn` fills every row with one mutation — `batch_size` isn't used: a mutation locks no row a batch would spare, and each one rewrites the parts it changes. |
| Row locks | `select_for_update()` locks the rows in ClickHouse Keeper with `transactions=true` and `keeper_hosts` — see [Transactions and locks](transactions-and-locks.md#row-locks). Without them it raises `UnSupportedError`, and the transactional outbox's relay doesn't start. |
| `NULL` from an aggregate over no rows | `Sum`, `Max`, `Min`, `Avg`, `StdDev` and `Variance` are written by their `-OrNull` form (`sumOrNull(...)`), so over no rows they give `None` as on any other database rather than the column type's default — see [`NULL` from an aggregate over no rows](#null-from-an-aggregate) |
| Correlated subqueries before 25.4 | An `EXISTS` correlated by equal columns runs as `(columns) IN (SELECT ...)`; any other correlated subquery raises `UnSupportedError` — see [Correlated subqueries before 25.4](#correlated-subqueries-before-25) |
| `LIKE ... ESCAPE` | A pattern escapes with a backslash, ClickHouse's own escape character. An empty text is contained in every text, as on PostgreSQL (`contains=""`, `contains=F("name")` of an empty name): ClickHouse matches no empty string by `'%%'`, so hare writes that pattern as `'%'`. |
| A backslash as a plain character of a quoted name | A name holding a backslash — a table, a column, an alias — is written with it doubled: ClickHouse reads a single one as an escape. |
| Case mapped character by character | `Upper()`, `Lower()` and the `i...` lookups convert a text holding a character whose full case mapping expands (`ß`, `ﬀ`, `İ`, a word-final `Σ`) one character at a time, as PostgreSQL does — see [Case mapped character by character](#case-mapped-character-by) |
| A recursive query adding rows by `UNION DISTINCT` | `with_recursive()` needs `max_depth`, which ends a cycle in the rows — see [Recursive queries and CTEs](../../querying/queryset-methods.md#recursive). Without it the walk raises `UnSupportedError`. |
| `NULL` first in a descending ordering | ClickHouse puts `NULL` last in both directions — see [`NULL` first in a descending ordering](#null-first-in-a) |
| A cursor of a transaction | `stream()` reads rows outside a transaction too: the server sends them as it computes them. Leaving the `async for` early closes the stream and gives the connection back. |

## <a id="an-update-of-rows"></a>An `UPDATE` of rows in place

`save()`, `QuerySet.update()` and `bulk_update()` run an `ALTER TABLE ... UPDATE` mutation, finished
before the call returns. A table of `ClickhouseTableOptions(lightweight_updates=True)` takes a
lightweight `UPDATE` instead (ClickHouse 25.7) — see [Models](models.md#lightweight-updates). A
`delete()` runs a lightweight `DELETE`, and an `ALTER TABLE ... DELETE` mutation when its condition reads
a `union()`/`intersection()`/`difference()` — the server's lightweight `DELETE` fails on one. A mutation
that fails is stopped (`KILL MUTATION`) before the error is raised: the server would retry it, fail every
later mutation of the table behind it, and could still apply it after the call raised. A mutation runs no
correlated subquery: an `update()` setting a field to a `Subquery` of `OuterReference`, or filtered or
deleted by one, raises `UnSupportedError`. A `bulk_update()` mutation writes at most 1000 rows
(`Features.max_bulk_update_rows`): it finds each row's values by scanning the list of rows, so a longer
one costs more per row and outgrows the server's AST limit. Its rows' keys are read by a subquery of the
list itself, the same on every replica: it runs with `allow_nondeterministic_mutations = 1`, which a
`ReplicatedMergeTree` table needs for a mutation reading a subquery — an `update()` filtered by a
subquery of another table is refused there by the server, as replicas might read different rows.

## <a id="returning"></a>`RETURNING`

The rows are read by their keys: an update's after it, a delete's before it, the values the server gave
inserted rows (`db_default`, `MATERIALIZED` columns) after the insert. A row changed by another writer
between the read and the write is read as it is at the read. `returning(old=...)` raises
`UnSupportedError` — the values before an update are not kept. A value an expression writes into an
integer or decimal field is computed by a `SELECT` before the write, and a value out of the field's range
raises `ValidationError` with no row written — `OperationalError` (`integer out of range`) for an integer
field set from a fraction, which is rounded as PostgreSQL rounds it.

## <a id="on-conflict"></a>`ON CONFLICT`

`bulk_create(ignore_conflicts=True)` and `bulk_create(update_fields=[...], on_conflict=[...])` read the
keys of the batch's conflict fields first: the rows already stored are skipped or updated with
`bulk_update()`, the rest inserted. As `ON CONFLICT DO UPDATE` does, an update bumps a row's
`Meta.optimistic_lock_field` version without comparing it with the object's, and of several objects
sharing one conflict key the later ones update the row after the insert, in order — the last one's values
win. `on_conflict` names fields declared unique for every row (a primary key, a unique field, a
`UniqueConstraint` without a condition) — others raise `QueryError`, as a database refuses them. Another
writer inserting a conflicting row between the read and the insert isn't seen.

## <a id="null-from-an-aggregate"></a>`NULL` from an aggregate over no rows

`Sum`, `Max`, `Min`, `Avg`, `StdDev` and `Variance` are written by their `-OrNull` form
(`sumOrNull(...)`), so over no rows they give `None` as on any other database rather than the column
type's default; `Count` gives `0`. In a query with `GROUP BY` a group holds at least one row, so an
aggregate without `_filter` keeps its plain, cheaper form (`sum(...)`). A sample deviation over one value
is `None`, as in SQL — ClickHouse gives NaN.

## <a id="correlated-subqueries-before-25"></a>Correlated subqueries before 25.4

An `EXISTS` correlated by equal columns — a filter or an exclusion across a to-many relation,
`<m2m>__isnull`, `Exists(...)` of `OuterReference` equalities — runs as `(columns) IN (SELECT ...)`, and
any other correlated subquery raises `UnSupportedError`. See
[Correlated subqueries](#correlated-subqueries).

## <a id="case-mapped-character-by"></a>Case mapped character by character

`Upper()`, `Lower()` and the `i...` lookups convert a text holding a character whose full case mapping
expands (`ß`, `ﬀ`, `İ`, a word-final `Σ`) one character at a time, as PostgreSQL does; any other text
goes through `upperUTF8()`/`lowerUTF8()` as a whole. Those are the server's own: ClickHouse 24.3 leaves
about 600 letters it has no mapping for unchanged (`µ`, `ı`, `ǅ`, `ς`, the Cherokee letters), 25.8 knows
every one.

## <a id="null-first-in-a"></a>`NULL` first in a descending ordering

ClickHouse puts `NULL` last in both directions; hare writes a descending ordering as `DESC NULLS FIRST`,
so `NULL` sorts as on PostgreSQL — last going up, first going down — and pages after a cursor,
`reverse()` and `last()` keep every row. A window's `ValueRange` with an offset orders by the value as a
`Float64`, a `NULL` as an infinity on the side `NULL` sorts to: ClickHouse's own range around a `NULL`
takes other rows into it. `ArrayAgg(order_by=...)` puts a `NULL` ordering value where the ordering does.

## <a id="long-in-lists"></a>Long `__in` lists

The server parses a list of literals slowly: 100,000 of them take most of a second. A `__in` or
`__not_in` list of at least 500 values of one plain type — UUIDs, integers, texts, dates, or rows of them
for a key of several columns — is bound as one parameter: a read sends the values beside its statement as
an external table (`x IN hare_set_1`), which the server reads as data in a few milliseconds; any other
statement (a mutation, a `DELETE`), and a read in a transaction, which reads no external table, writes
them as a list of literals. The SQL of the query is the same for a list of any length, so it keeps one
plan.

Indexes are data skipping indexes: `Index(fields=...)` creates a `minmax` index of granularity 1, and
`hare.dialects.clickhouse.indexes` has the other types — see [Models](models.md#data-skipping-indexes).
`hare inspectdb` and `hare drift` read the tables, columns, primary keys, comments, skipping indexes,
table options, views and dictionaries from the `system` tables.

## <a id="correlated-subqueries"></a>Correlated subqueries

ClickHouse runs correlated subqueries from 25.4 on (behind `allow_experimental_correlated_subqueries`
until 25.7, which hare sets). Some of them it still computes wrongly or refuses, so on every version:

- **`EXISTS`** correlated by equal columns — of a composite key's row of them too — is written as
  `(columns) IN (SELECT ...)` — the server refuses its own correlated `EXISTS` in a join's `ON` and over
  a `UNION DISTINCT` (25.8). An `EXISTS` of a `union()` is written as the `EXISTS` of either part, each
  so. One correlated by more than equalities in its `WHERE` (`version__gt=OuterReference("version")`, as
  [`LatestVersions`](../../integrations/request-queries.md#latestversions) is) runs as the server's own
  `EXISTS`; in an `UPDATE` or `DELETE`, or correlated outside its `WHERE`, it raises `UnSupportedError`.
  A shape the server doesn't run yet raises `UnSupportedError` with its message.
- **A grouping by a correlated subquery** (`values("author_name").annotate(...)` of a `Subquery` with
  `OuterReference`) raises `UnSupportedError` — the server groups by none.
- **A subquery ordering or slicing its own rows** (`Subquery(... .order_by("-created")[:1])` with
  `OuterReference`) raises `UnSupportedError` — the server refuses it.
- **A subquery in `ORDER BY`, or selected beside a `WHERE`** is computed in a derived table: the rows are
  selected with no `WHERE`, the conditions and the sort keys as hidden columns of it, and the query
  around it filters, orders and slices them. The SQL is longer; the result is the one the query asks for.
- An `UPDATE` or `DELETE` whose condition holds a correlated subquery raises `UnSupportedError` — a
  mutation runs none.
- **An `OuterReference` across a relation** (`OuterReference("author__name")`) reads a column of a table
  joined to the query around the subquery, which the server correlates by none: it raises
  `UnSupportedError`. Reach the relation inside the subquery and correlate by a column of the outer table
  itself (`Author.objects.filter(books__id=OuterReference("id"))`), or read the related field without a
  subquery (`F("author__name")`).
- A table a subquery joins is read through a derived table, as the subquery's own one: an aliased table
  answers to its own name too on ClickHouse, and the subquery's would take the outer query's name.

## <a id="text-order"></a>Text order

ClickHouse orders and compares text by its UTF-8 bytes, as SQLite does by default; PostgreSQL by the
collation of its database, whose order of a locale puts `a` next to `A` and letters with accents next to
their base letters. `order_by()`, `__gt`/`__lt` and `Min`/`Max` of a `CharField`, a `TextField` or a
`CharEnumField` (compared by its label) follow the database's order; `Collate(...)` asks a database for
an order of its own.

## <a id="json"></a>JSON

On ClickHouse 25.3 and later a `JSONField` is a `JSON` column, its paths stored as typed subcolumns; on
an older server, and over clickhouse-driver, which reads no `JSON` column, it is a `String` column of
JSON text, read by the `JSON*` functions. Which one a connection has is known once the connection opens —
`hare drift` and the migrations compare and create the column of that connection. A table made over one
driver is read, written and filtered over the other as well; `hare drift` over the other reports the
column's type (`type is JSON, the model expects String`) — the two keep a `null` differently, below. The
`JSON` type stores less than JSON text does:

- Only an object is stored: a list, a string or a number at the top level raises `ValidationError` before
  anything is sent.
- `null` inside an object and an empty object aren't stored: `{"a": null, "b": 1}` reads back as
  `{"b": 1}`. So `field__path=None`, a `None` in `field__path__in=[...]` and `field__path__isnull=True`
  match a path with no value — a `null` there can't be told apart, and a row whose whole value is NULL
  has none —, `field__path__not=None` and a `None` in `field__path__not_in=[...]` leave it out, and a
  comparison of the path (`field__path__lt=10`) doesn't match it, where PostgreSQL's `jsonb` order puts
  `null` before every value. A `String` column — before 25.3, or over clickhouse-driver — keeps the
  `null` and compares as PostgreSQL does.
- The order of keys isn't kept.
- A dot in a key stays part of the key from ClickHouse 25.8 on (`Features.escapes_json_key_dots`): each
  connection sets `json_type_escape_dots_in_keys`, and hare writes the dot as the server escapes it,
  `%2E` — an `update()`'s mutation, which reads the JSON with the server's own settings, too. A key
  holding the text `%2E` itself raises `ValidationError`: the server would read it back as a dot. Before
  25.8 the type stores a dotted key as a nested path, so a key holding a dot raises `ValidationError`
  before anything is sent.
- An array of integers and floats holds them all as floats: `[1, 2.5]` reads back as `[1.0, 2.5]`. A
  boolean stays one, and a string stays a string even when it looks like a date or a moment
  (`"2024-01-02"`): each connection sets `input_format_try_infer_dates` and
  `input_format_try_infer_datetimes` off. But `update()` writes by a mutation, which reads the JSON with
  the server's own settings: an array of numbers and booleans it writes holds `true` as `1`, and a
  date-like string is read back as a `date` or a `datetime` unless the server's default profile sets
  those two settings to `0`.

Key paths, `__has_key`, `__has_keys`, `__has_any_keys`, `__contains` and `__contained_by` work on both.
`__contains` and `__contained_by` compare as PostgreSQL's `jsonb` does — an object by its keys, an array
by its elements in any order — written from the value given: its keys and scalars are bound, and the SQL
follows its shape. A migration moves a `String` column to `JSON` with `MODIFY COLUMN` when the server has
the type.

On a server with the `JSON` type — also over clickhouse-driver, whose column is a `String` one — a
`JSONField` compared whole (`=`, `__not`, `__in`, `__not_in`) is compared as `jsonb` is: the key order
and the spelling of a number (`2` and `2.0`) don't matter. On an older server the JSON text is compared
as it was written.

`Cast(..., TextField())` and `Concat` write JSON with a space after each `,` and `:`, as `jsonb` does,
but keep the column's key order (the `JSON` type's own, the written one in a `String` column) and its
spelling of a number (`1e+20`, `12.0`) — PostgreSQL writes an object's shorter keys first and a number in
plain digits.

A `BinaryField` is a `String` column holding the bytes' lowercase hex: clickhouse-connect reads a
`String` as text and writes bytes that aren't UTF-8 as their hex, so raw bytes couldn't be read back. The
hex reads back exactly over both drivers and orders as the bytes do; `unhex()` gives the bytes in SQL.

## <a id="bulk-loads"></a>Bulk loads

`bulk_create()` sends its rows in one binary insert — ClickHouse's `Native` format, a column after a
column — instead of an `INSERT ... VALUES` text: no literal is written for a value, and the server parses
no SQL. That is how rows are meant to reach ClickHouse, and the reason to write many rows by
`bulk_create()` rather than by `create()` in a loop.

- `batch_size` splits the objects into as many inserts; without it they go in one.
- The values are the ones `create()` writes: a moment is the instant it stands for (a naive one under
  `use_timezone=False` the instant of its local wall clock, read back as that wall clock), a JSON value
  is its text or its object.
- Before the insert the batch's keys are checked by one `SELECT` per declared uniqueness and per
  relation, the keys sent as an external table (see [Long `__in` lists](#long-in-lists)) — see
  [Models](models.md#uniqueness-and-relations); rows repeating a key inside the batch are found without a
  query.
- The insert is reported to [observers](../../observability/observers.md), spans and the query counter as
  `INSERT INTO <table> (<columns>) FORMAT Native` — the rows are in no SQL text.
- `use_copy=True` asks for the same load and changes nothing here. `returning=True` reads the rows back
  by their keys after the insert; the conflict options read the stored keys before it.
- With `async_insert=true` the server collects the rows of many inserts before it writes them; with
  `wait_for_async_insert=false` the call returns before they are written and reports no error.
