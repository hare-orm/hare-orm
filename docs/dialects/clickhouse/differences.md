# ClickHouse: what runs differently

ClickHouse lacks parts of SQL hare would otherwise rely on: unique and foreign key constraints, an
`UPDATE` in place, `RETURNING`, `ON CONFLICT`, row locks, a count of the rows a write changed.
Transactions and generated keys exist only on a server with a ClickHouse Keeper. Each row below says what
hare does: where ClickHouse has another form of the same SQL, hare writes it; where the language has none,
the call is refused before any SQL is sent.

| ClickHouse lacks | hare |
|---|---|
| Transactions, by default | `atomic()` raises `UnSupportedError` unless the connection asks for them with `transactions=true` — see [Transactions and locks](transactions-and-locks.md). Without, hare's own multi-statement writes run one statement after another, a failure leaving the statements before it written. |
| Savepoints | A nested `atomic()` joins the transaction it is nested in; an error leaving it rolls the whole transaction back. |
| Generated keys | With a ClickHouse Keeper, the keys are taken from a series before the rows are written (`generateSerialID`) — see [Models](models.md#keys). Without one, a model whose key the database generates is refused when it is bound. |
| Unique and foreign key constraints | A uniqueness or a relation with `db_constraint=True` is refused — a model when it is bound (`ConfigurationError`), a migration declaring a uniqueness before its first statement (`UnSupportedError`); see [Models](models.md#uniqueness-and-relations). |
| A `CHECK` passing a row its condition gives `NULL` | ClickHouse refuses such a row (`IntegrityError`): `CheckConstraint(check=Q(price__gte=0))` refuses `price=None` — a check meant to take `NULL` names it, `Q(price__isnull=True) \| Q(price__gte=0)`. A check a migration adds checks only the rows written later — see [Operations](../../migrations/operations.md). |
| An `UPDATE` of rows in place | `save()`, `QuerySet.update()` and `bulk_update()` run an `ALTER TABLE ... UPDATE` mutation, finished before the call returns — see [An `UPDATE` of rows in place](#an-update-of-rows) |
| A count of the rows an UPDATE or DELETE changed | `update()`, `delete()` and `bulk_update()` return `None` (`Features.reports_written_row_count`) — see [A count of written rows](#written-row-count) |
| `RETURNING` | `returning()` of an `update()` or a `delete()` and `bulk_create(returning=True)` raise `UnSupportedError`; the values the server gives a written row stay unread on the instance — see [`RETURNING`](#returning) |
| `ON CONFLICT` | `bulk_create()` with `ignore_conflicts`, `update_fields`, `on_conflict` or `on_conflict_constraint` raises `UnSupportedError` (`Features.supports_insert_conflicts`); the transactional outbox, which writes its events so, is refused when its model is bound — see [Outbox](../../integrations/outbox.md). |
| Batches of an UPDATE | `BackfillColumn` fills every row with one mutation — `batch_size` isn't used: a mutation locks no row a batch would spare, and each one rewrites the parts it changes. |
| Row locks | `select_for_update()` and `atomic(lock_timeout=...)` raise `UnSupportedError` — see [Transactions and locks](transactions-and-locks.md). |
| `NULL` from an aggregate over no rows | An aggregate whose result over no rows would be a value no row holds is written by its `-OrNull` form (`maxOrNull(...)`) and gives `None`; `Sum` gives ClickHouse's own `0` — see [`NULL` from an aggregate over no rows](#null-from-an-aggregate) for the list |
| `LIKE ... ESCAPE` | A pattern escapes with a backslash, ClickHouse's own escape character. An empty text is contained in every text, as on PostgreSQL (`contains=""`, `contains=F("name")` of an empty name): ClickHouse matches no empty string by `'%%'`, so hare writes that pattern as `'%'`. |
| A plain `UPDATE`, and a refused `NULL`, in raw SQL | SQL you write (`RunSQL`, `raw()`, `execute()`) runs as ClickHouse takes it: an `UPDATE` statement needs the table's `enable_block_number_column` setting (`ALTER TABLE ... UPDATE` doesn't), and a `NULL` inserted into a column that isn't `Nullable` is written as the type's empty value (`0`, `''`). hare's own writes refuse such a `None`. |
| A backslash as a plain character of a quoted name | A name holding a backslash — a table, a column, an alias — is written with it doubled: ClickHouse reads a single one as an escape. |
| A recursive query adding rows by `UNION DISTINCT` | `with_recursive()` needs `max_depth`, which ends a cycle in the rows — see [Recursive queries and CTEs](../../querying/queryset-methods.md#recursive). Without it the walk raises `UnSupportedError`. |
| `NULL` first in a descending ordering | ClickHouse puts `NULL` last in both directions — see [`NULL` first in a descending ordering](#null-first-in-a) |
| A cursor of a transaction | `stream()` reads rows outside a transaction too: the server sends them as it computes them. Leaving the `async for` early closes the stream and gives the connection back. |
| A zone's UTC offset with seconds | ClickHouse keeps a zone's offset in whole minutes: a local mean time before a zone's standard time (Moscow before 1919, `+02:30:17`) is read without its seconds — `Extract("second")`, `Trunc()` and `__time` of such a moment in that zone (or with `use_timezone=False` in it) differ from PostgreSQL's by those seconds. |

## <a id="an-update-of-rows"></a>An `UPDATE` of rows in place

`save()`, `QuerySet.update()` and `bulk_update()` run an `ALTER TABLE ... UPDATE` mutation, finished
before the call returns. A table of `ClickhouseTableOptions(lightweight_updates=True)` takes a
lightweight `UPDATE` instead — see [Models](models.md#lightweight-updates). A
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

## <a id="written-row-count"></a>A count of written rows

ClickHouse reports no count of the rows a mutation or a lightweight `UPDATE`/`DELETE` changed, so
`update()`, `delete()` and `bulk_update()` return `None`; read the rows to learn what changed. A delete hare
runs itself — a cascade in Python, a model overriding `delete()` — counts the rows it read for it. Without
a count:

- a `save()` or `delete()` of a row another writer removed goes unnoticed — no `DoesNotExist` is raised;
- a model with `Meta.optimistic_lock_field` is refused when it is bound: a stale version would go
  unnoticed;
- a `bulk_update()` counts every object as written, and applies its version, its snapshot of changed
  fields and its `auto_now` values to all of them.

## <a id="returning"></a>`RETURNING`

`update().returning()`, `delete().returning()` and `bulk_create(returning=True)` raise
`UnSupportedError` before any SQL is sent. A value the server gives a row a `save()` or `create()` writes
— a `db_default`, a `MATERIALIZED` column — stays unread on the instance: the field holds
`DatabaseDefault`, a computed field isn't loaded, and `refresh_from_db()` reads them. A key the server
generates is taken before the insert (see [Models](models.md#keys)); a model whose key is left to a
`db_default` is refused when a row is written.

## <a id="null-from-an-aggregate"></a>`NULL` from an aggregate over no rows

`Max`, `Min`, `Avg`, `StdDev`, `Variance` and ClickHouse's own `ArgMax`, `ArgMin`, `AnyValue`,
`AnyLast`, `Quantile` and `Median` (`exact=True` too) are written by their `-OrNull` form
(`maxOrNull(...)`), so over no rows they give `None` rather than the column type's default; `Count` gives
`0` and `Sum` gives ClickHouse's own `0` (`0.00` for a decimal). In a query with `GROUP BY` a group holds
at least one row, so an aggregate without `_filter` keeps its plain, cheaper form (`max(...)`). A sample
deviation over one value is `None`, as in SQL — ClickHouse gives NaN.

## <a id="results"></a>Results that are ClickHouse's own

hare doesn't make ClickHouse give PostgreSQL's result. It refuses a value ClickHouse would silently turn
into another value or leave unreadable, writes the forms it stores itself (an enum by its label, an
address as an `IPv6`, a JSON path) and supplies its functions by ClickHouse's own means; everything else is
ClickHouse's own, and differs from PostgreSQL's:

- **Division and remainder by zero** give `inf` or `nan` for floats and ClickHouse's own error for
  integers; **a math function outside its domain** (`Sqrt(-1)`, `Ln(0)`) gives `nan` or `inf`. hare checks
  none of them. `MOD` and a negative zero are ClickHouse's own.
- **Decimal quotients.** A division, `Avg()`, `StdDev()` and `Variance()` of decimals are computed exactly
  and cut, not rounded, to 20 digits after the point, where PostgreSQL's numeric has scale rules of its
  own. `Round()` is ClickHouse's `round()`.
- **Counts, lengths, row numbers and ranks** (`Count()`, `Length()`, `StrIndex()`, and the window
  functions `RowNumber()`, `Rank()`, `DenseRank()`, `NTile()`) are signed `Int64` values: ClickHouse's
  own `UInt64` finds no common type with a signed integer in `Coalesce()`, `Case()` or `Greatest()`.
- **Integer arithmetic** a query reads past its type's range is refused (`integer out of range`); a
  value `update(n=F("n") + 1)` writes is converted by ClickHouse into the column's type, wrapping
  around past its range (`2147483647 + 1` stored as `-2147483648` in an `Int32`). `/` of two integers
  gives the exact quotient, a fraction too (`Features.divides_integers_exactly`) — the expression's output
  field is a `FloatField`. A decimal an expression computes with more places than the field keeps is cut
  by ClickHouse, not rounded: `update(price=F("price") + Decimal("0.005"))` leaves `12.34` where
  PostgreSQL and SQLite write `12.35`.
- **`Cast()`** to an integer cuts the fraction off, where PostgreSQL rounds; an integer past the target
  field's range raises. A decimal target rounds half away from zero to its places, and a float becomes a
  decimal by its shortest digits. A cast to a boolean is ClickHouse's `toBool()`, which accepts `yes`,
  `no`, `on` and `off` too. A cast to text is `toString()`: `1e308`, `-inf`, a decimal without trailing
  zeros, a moment as `2024-01-02 03:04:05.678900`. A number padded with spaces (`' 42 '`) isn't read, and
  text that isn't a value of the type raises. A `CharField`'s `max_length` cuts nothing.
- **`Concat()`** joins the text ClickHouse gives a column's value (`toString()`); a naive datetime
  (`use_timezone=False`) is written at the machine's local zone. A Python value given as a literal is its
  Python text: a boolean `true` or `false`, a float by `repr()` (`1000000000000000.0`), a date or datetime
  as ISO text. A text lookup (`contains`, `startswith`, `iexact`, ...) on a boolean, float or decimal
  column matches the text ClickHouse gives the value.
- **Case mapping.** `Upper()`, `Lower()` and the `i...` lookups go through `upperUTF8()` and
  `lowerUTF8()`, the server's own.
- **`Substr`, `Chr` and `Ord`** are `substringUTF8()` (a start of 0 is refused, a negative start counts
  from the end, a negative length leaves characters off the end), `char()` (a code point no character has
  is refused, 0 gives `\x00`) and the code point of the first character for `Ord`. The text
  functions have no 32-bit limit on a length, a position or a count, and take an expression of any type;
  a literal of the wrong type is refused.
- **`FIRST_VALUE` and `LAST_VALUE`** are ClickHouse's own, as is the order of a UUID (lower 64 bits
  first — a `uuid7` key doesn't sort by time, so order by a date column) and of an `IPv6` address. An enum
  orders by its label, which hare stores that way. A time of day orders and compares as its stored text:
  `10:00:00+03:00` sorts after `08:00:00+00:00`, though it is the earlier moment.
- **A NaN in a `FloatField`** is stored. **A null byte** in a text or a JSON value is stored as ClickHouse
  stores it.
- **Errors.** A decimal past its digits raises `the value has more digits than the decimal holds`; making
  a column `NOT NULL` while it holds `NULL` raises `column X of table Y holds NULL`.

## <a id="null-first-in-a"></a>`NULL` first in a descending ordering

ClickHouse puts `NULL` last in both directions; hare writes a descending ordering as `DESC NULLS FIRST`,
so `NULL` sorts last going up and first going down, and pages after a cursor, `reverse()` and `last()`
keep every row. A window's `ValueRange` with an offset orders by the value as a
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

ClickHouse runs correlated subqueries, but computes some of them wrongly or refuses them:

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
- **A subquery selecting `Count`, `Sum` or another aggregate of its rows, correlated by a column rows of
  the query around it share** (`Book.objects.annotate(n=Subquery(Award.objects.filter(author=OuterReference(
  "author"))...))`) raises `UnSupportedError` (`Features.supports_correlated_aggregates`): the server reads
  each of its rows once per outer row holding the same value, and counts them that many times. Correlated
  by the outer primary key or a unique field (`OuterReference("pk")`) it runs, as do `Max`, `Min` and `Avg`
  by any column. Count over the relation with `annotate(Count("books"))` instead.
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

## <a id="server-faults"></a>Faults of a server version

hare doesn't work around a fault of a ClickHouse version; the server's own error or result stands:

- **A column made `Nullable`** (`AlterField` to `null=True`): ClickHouse before 26.3 reads the NULL map
  the parts stored before the change lack as every value NULL — `filter(title__isnull=True)` matches every
  such row. On these versions rewrite the column after the migration yourself:
  `ALTER TABLE "article" UPDATE "title" = "title" WHERE 1` (in a `RunSQL` after the `AlterField`).
- **An `update()` setting a constant value under a negation that folds into a constant**
  (`exclude(owner__isnull=True).update(balance=0)` of a non-nullable `owner`): ClickHouse before 26.3
  refuses it with *Cannot convert column ... because it is non constant in source stream*.
- **`Count()` of a nullable column in a window** (`Window(Count("rating"))`): every version up to 26.9
  refuses it with *Not found column ... in block* (`NOT_FOUND_COLUMN_IN_BLOCK`).
- ClickHouse 25.12 refuses a query that joins a many-to-many relation for an aggregate next to a
  `select_related()` object (`Player.objects.annotate(Count("skills")).select_related("team")`) with
  *Cannot fold actions for projection* (`LOGICAL_ERROR`) — its join reordering; 25.10 and 26.x run it.

## <a id="text-order"></a>Text order

ClickHouse orders and compares text by its UTF-8 bytes, as SQLite does by default; PostgreSQL by the
collation of its database, whose order of a locale puts `a` next to `A` and letters with accents next to
their base letters. `order_by()`, `__gt`/`__lt` and `Min`/`Max` of a `CharField`, a `TextField` or a
`CharEnumField` (compared by its label) follow the database's order; `Collate(...)` asks a database for
an order of its own — on ClickHouse in `order_by()` alone (`order_by(Collate("name", "en"))`, written
`ORDER BY name ASC COLLATE 'en'`), since the server takes a collation nowhere else.

## <a id="json"></a>JSON

A `JSONField` is a `JSON` column, its paths stored as typed subcolumns (the outbox's `payload` and
`headers` excepted — see [Outbox](../../integrations/outbox.md#encoding)); over clickhouse-driver, which
reads no `JSON` column, it is a `String` column of JSON text, read by the `JSON*` functions. Which one a
connection has follows from its driver, before it opens — `hare drift`, `hare sqlmigrate` and the migrations
compare, print and create the column of that connection. A table made over one driver is read, written and
filtered over the other as well;
`hare drift` over the other reports the column's `AlterField` — the two
keep a `null` differently, below. The `JSON` type stores less than JSON text does, and a value it would
read back otherwise raises `ValidationError` before anything is sent — a `String` column, over
clickhouse-driver, keeps them all:

- Only an object is stored: a list, a string or a number at the top level is refused.
- A key holding `None` or an empty object is refused, at any depth (`{"a": None}`,
  `{"items": [{"b": {}}]}`) — the type keeps no such path. A `None` or an empty object inside an array
  (`[None, 1]`, `[{}]`) is kept. So `field__path=None`, a `None` in `field__path__in=[...]` and
  `field__path__isnull=True` match a path with no value — a row whose whole value is NULL has none —,
  `field__path__not=None` and a `None` in `field__path__not_in=[...]` leave it out, and a comparison of
  the path (`field__path__lt=10`) doesn't match it.
- An integer outside the range of an `Int64` and a `UInt64` (`-2**63` to `2**64 - 1`) is refused — the type
  holds it as a float.
- The order of keys isn't kept.
- A dot in a key stays part of the key: each connection sets `json_type_escape_dots_in_keys`, and hare writes
  the dot as the server escapes it, `%2E` — an `update()`'s mutation, which reads the JSON with the server's
  own settings, too. A key holding the text `%2E` itself raises `ValidationError`: the server would read it
  back as a dot.
- An array of integers and floats holds them all as floats: `[1, 2.5]` reads back as `[1.0, 2.5]`; one whose
  integers (its nested arrays' included) go past `2**53`, which a float doesn't hold exactly, is refused. A
  boolean stays one, and a string stays a string even when it looks like a date or a moment
  (`"2024-01-02"`): each connection sets `input_format_try_infer_dates` and
  `input_format_try_infer_datetimes` off. But `update()` writes by a mutation, which reads the JSON with
  the server's own settings: an array of numbers and booleans it writes holds `true` as `1`, and a
  date-like string is read back as a `date` or a `datetime` unless the server's default profile sets
  those two settings to `0`.

Key paths, `__has_key`, `__has_keys` and `__has_any_keys` work on both; `__filter`, `__contains` and
`__contained_by` don't — ClickHouse's JSON has no containment, and they raise `UnSupportedError`; filter
by key paths instead. `__has_key`, `__has_keys` and `__has_any_keys` find a key of an object only
(`JSONHas()`). A migration moves a `String` column to `JSON` with `MODIFY COLUMN`.

A `JSONField` compared whole (`=`, `__not`, `__in`, `__not_in`) on a `JSON` column has the value cast to
`JSON` and compared by ClickHouse, so the key order doesn't matter; `__in` compares each value so, joined
by `OR`. A JSON column holds an object only: a compared value that is no object (a list, a string, a
number) raises `ValidationError`. On a `String` column of JSON text — over clickhouse-driver — the JSON
text is compared as it was written.

An ordering of a path (`__gt`, `__gte`, `__lt`, `__lte`, `__range`, `order_by()`) uses the `Dynamic` value
ClickHouse reads of the JSON text (`JSONExtract(..., 'Dynamic')`, with the session setting
`allow_suspicious_types_in_order_by=1`). ClickHouse orders a `Dynamic` by the name of its type first and
by the value second: a `Float64` `2.5` comes before an `Int64` `9`, a boolean before the numbers and the
strings after them. Comparing a path that holds both strings and numbers raises ClickHouse's
`NO_COMMON_TYPE`. `jsonb`'s order of objects, arrays, booleans, numbers, strings and `null` is
PostgreSQL's alone, and a comparison with an object or an array is ClickHouse's own.

`Cast(..., TextField())` and `Concat` of a `JSONField` give the text ClickHouse writes: the column's key
order (the `JSON` type's own, the written one in a `String` column) and its spelling of a number
(`1e+20`, `12.0`). A text lookup of a path matches the text ClickHouse reads of the value.
`JSONObject()` and `JSONArray()` are written by `toJSONString()`: a decimal without trailing zeros, a
`DateTime64` as `2024-02-29 13:45:06.789000`, a `NaN` as `null`, bytes as the hex the column holds.

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
- The insert is reported to [observers](../../observability/observers.md), spans and the query counter as
  `INSERT INTO <table> (<columns>) FORMAT Native` — the rows are in no SQL text.
- `use_copy=True` asks for the same load and changes nothing here. `returning=True` and the conflict
  options raise `UnSupportedError`.
- With `async_insert=true` the server collects the rows of many inserts before it writes them; with
  `wait_for_async_insert=false` the call returns before they are written and reports no error.
