# What runs differently on ClickHouse

ClickHouse lacks parts of SQL hare would otherwise rely on: unique and foreign key constraints, an
`UPDATE` in place, `RETURNING`, `ON CONFLICT`, row locks. Transactions and generated keys exist only
on a server with a ClickHouse Keeper. Each row below says what hare does instead.

| ClickHouse lacks | hare |
|---|---|
| Transactions, by default | `atomic()` raises `UnSupportedError` unless the connection asks for them with `transactions=true` — see [Transactions and locks](transactions-and-locks.md). Without, hare's own multi-statement writes run one statement after another, a failure leaving the statements before it written. |
| Savepoints | A nested `atomic()` joins the transaction it is nested in; an error leaving it rolls the whole transaction back. |
| Generated keys | On ClickHouse 25.1 and later with a Keeper, the keys are taken from a series before the rows are written (`generateSerialID`) — see [Models](models.md#keys). Elsewhere, a model whose key the database generates is refused when it is bound. |
| Unique and foreign key constraints | hare checks the uniqueness and the relations a model declares before it writes — see [Models](models.md#uniqueness-and-relations). |
| An `UPDATE` of rows in place | `save()`, `QuerySet.update()` and `bulk_update()` run an `ALTER TABLE ... UPDATE` mutation, finished before the call returns. A table of `ClickhouseTableOptions(lightweight_updates=True)` takes a lightweight `UPDATE` instead (ClickHouse 25.7) — see [Models](models.md#lightweight-updates). |
| A row count of an UPDATE or DELETE | The rows a mutation or a DELETE matches are counted right before it — another writer changing them in between changes the count. |
| `RETURNING` | The rows are read by their keys: an update's after it, a delete's before it, the values the server gave inserted rows (`db_default`, `MATERIALIZED` columns) after the insert. A row changed by another writer between the read and the write is read as it is at the read. `returning(old=...)` raises `UnSupportedError` — the values before an update are not kept. A value an expression writes into an integer or decimal field is computed by a `SELECT` before the write, and a value out of the field's range raises `ValidationError` with no row written. |
| `ON CONFLICT` | `bulk_create(ignore_conflicts=True)` and `bulk_create(update_fields=[...], on_conflict=[...])` read the keys of the batch's conflict fields first: the rows already stored are skipped or updated with `bulk_update()`, the rest inserted. Another writer inserting a conflicting row between the read and the insert isn't seen. |
| Row locks | `select_for_update()` locks the rows in ClickHouse Keeper with `transactions=true` and `keeper_hosts` — see [Transactions and locks](transactions-and-locks.md#row-locks). Without them it raises `UnSupportedError`, and the transactional outbox's relay doesn't start. |
| `NULL` from an aggregate over no rows | `Sum`, `Max`, `Min`, `Avg`, `StdDev` and `Variance` are written by their `-OrNull` form (`sumOrNull(...)`), so over no rows they give `None` as on any other database rather than the column type's default; `Count` gives `0`. In a query with `GROUP BY` a group holds at least one row, so an aggregate without `_filter` keeps its plain, cheaper form (`sum(...)`). A sample deviation over one value is `None`, as in SQL — ClickHouse gives NaN. |
| Correlated subqueries before 25.4 | An `EXISTS` correlated by equal columns — a filter or an exclusion across a to-many relation, `<m2m>__isnull`, `Exists(...)` of `OuterReference` equalities — runs as `(columns) IN (SELECT ...)`, and any other correlated subquery raises `UnSupportedError`. See [Correlated subqueries](#correlated-subqueries). |
| `LIKE ... ESCAPE` | A pattern escapes with a backslash, ClickHouse's own escape character. |
| A cursor of a transaction | `stream()` reads rows outside a transaction too: the server sends them as it computes them. Leaving the `async for` early closes the stream and gives the connection back. |

## <a id="long-in-lists"></a>Long `__in` lists

The server parses a list of literals slowly: 100,000 of them take most of a second. A `__in` or
`__not_in` list of at least 500 values of one plain type — UUIDs, integers, texts, dates, or rows of
them for a key of several columns — is bound as one parameter: a read sends the values beside its
statement as an external table (`x IN hare_set_1`), which the server reads as data in a few
milliseconds; any other statement (a mutation, a `DELETE`) writes them as a list of literals. The SQL
of the query is the same for a list of any length, so it keeps one plan.

Indexes are data skipping indexes: `Index(fields=...)` creates a `minmax` index of granularity 1, and
`hare.dialects.clickhouse.indexes` has the other types — see [Models](models.md#data-skipping-indexes).
`hare inspectdb` and `hare drift` read the tables, columns, primary keys, comments, skipping indexes,
table options, views and dictionaries from the `system` tables.

## <a id="correlated-subqueries"></a>Correlated subqueries

ClickHouse runs correlated subqueries from 25.4 on (behind `allow_experimental_correlated_subqueries`
until 25.7, which hare sets). Some of them it still computes wrongly or refuses, so on every version:

- **`EXISTS`** correlated by equal columns is written as `(columns) IN (SELECT ...)` — the server's
  own correlated `EXISTS` misses rows (25.8).
- **A subquery ordering or slicing its own rows** (`Subquery(... .order_by("-created")[:1])` with
  `OuterReference`) raises `UnSupportedError` — the server refuses it.
- **A subquery in `ORDER BY`, or selected beside a `WHERE`** is computed in a derived table: the rows
  are selected with no `WHERE`, the conditions and the sort keys as hidden columns of it, and the
  query around it filters, orders and slices them. The SQL is longer; the result is the one the
  query asks for.
- An `UPDATE` or `DELETE` whose condition holds a correlated subquery raises `UnSupportedError` — a
  mutation runs none.

## <a id="json"></a>JSON

On ClickHouse 25.3 and later a `JSONField` is a `JSON` column, its paths stored as typed
subcolumns; on an older server it is a `String` column of JSON text, read by the `JSON*` functions.
The `JSON` type stores less than JSON text does:

- Only an object is stored: a list, a string or a number at the top level raises `ValidationError`
  before anything is sent.
- `null` inside an object and an empty object aren't stored: `{"a": null, "b": 1}` reads back as
  `{"b": 1}`. So `field__path=None` and `field__path__isnull=True` match a path with no value — a
  `null` there can't be told apart.
- The order of keys isn't kept.

Key paths, `__has_key`, `__has_keys`, `__has_any_keys`, `__contains` and `__contained_by` work on
both. `__contains` and `__contained_by` compare as PostgreSQL's `jsonb` does — an object by its keys,
an array by its elements in any order — written from the value given: its keys and scalars are
bound, and the SQL follows its shape. A migration moves a `String` column to `JSON` with
`MODIFY COLUMN` when the server has the type.

## <a id="bulk-loads"></a>Bulk loads

`bulk_create()` sends its rows in one binary insert — ClickHouse's `Native` format, a column after a
column — instead of an `INSERT ... VALUES` text: no literal is written for a value, and the server
parses no SQL. That is how rows are meant to reach ClickHouse, and the reason to write many rows by
`bulk_create()` rather than by `create()` in a loop.

- `batch_size` splits the objects into as many inserts; without it they go in one.
- The values are the ones `create()` writes: a naive moment is its UTC wall clock, a JSON value is its
  text or its object.
- Before the insert the batch's keys are checked by one `SELECT` per declared uniqueness and per
  relation, the keys sent as an external table (see [Long `__in` lists](#long-in-lists)) — see [Models](models.md#uniqueness-and-relations); rows repeating a
  key inside the batch are found without a query.
- The insert is reported to [observers](../../observability/observers.md), spans and the query
  counter as `INSERT INTO <table> (<columns>) FORMAT Native` — the rows are in no SQL text.
- `use_copy=True` asks for the same load and changes nothing here. `returning=True` reads the rows
  back by their keys after the insert; the conflict options read the stored keys before it.
- With `async_insert=true` the server collects the rows of many inserts before it writes them; with
  `wait_for_async_insert=false` the call returns before they are written and reports no error.
