# Raw SQL

SQL written by hand, at four levels: model rows read by a whole statement (`raw()`), a fragment inside an
ORM query (`RawSQL`), a statement assembled with the query builder (`execute_sql()`), and any statement's
result as a table of columns and rows (`execute_described()`). Every value that varies goes in as a bind
parameter.

## <a id="raw"></a>Model rows from SQL: `raw()`

```python
class QuerySet:
    def raw(self, sql: str, parameters: Sequence[Any] = ()) -> RawSQLQuery[TModel]: ...
```

```python
books = await Book.objects.raw("SELECT * FROM book WHERE title LIKE %s", ["%dune%"])
```

`raw()`: any value that varies per call belongs in `parameters` (substituted at each `%s` placeholder in
`sql` as a real bind parameter), never string-interpolated into `sql` directly — see [`RawSQL`](#rawsql)
below. The query must select the primary key column(s), otherwise `FieldError`. A selected column that is
neither a column nor a field of the model becomes an attribute of each instance, like an annotation
(`SELECT b.*, b.price * 2 AS doubled ...` gives `book.doubled`, `COUNT(b.id) AS book_count` gives
`author.book_count`), holding the driver's value. A column name returned more than once
(`SELECT b.*, a.id ...`) raises `QueryError` — alias the extra column.

## <a id="rawsql"></a>`RawSQL`

```python
class RawSQL(ArithmeticOperators, Plannable, Term):
    def __init__(self, sql: str, parameters: Sequence[Any] = ()) -> None: ...
```

```python
from hare.query.expressions import RawSQL

await Book.objects.annotate(next_id=RawSQL("id + 1")).order_by("-next_id")
await Book.objects.filter(rating__gt=0).annotate(bumped=RawSQL("rating + %s", [10])).order_by("-bumped")
```

Any value that varies per call belongs in `parameters`, never string-interpolated into `sql` directly.
Each `%s` placeholder in `sql` (left to right) is substituted with the matching `parameters` entry as a
real bind parameter — through the same mechanism every other value in a hare query goes through, so the
driver receives it out-of-band from the SQL text, not inlined into it. The number of `%s` placeholders
must exactly match `len(parameters)`, checked immediately when `RawSQL(...)` is constructed.

A list (or tuple) parameter is bound as one array, on a database with array parameters (PostgreSQL):
`RawSQL("SELECT unnest(%s::int[])", [ids])`, `id = ANY(%s)`. Elsewhere it raises `UnSupportedError`. A
dict or set raises `QueryError` when `RawSQL(...)` is constructed — pass `json.dumps(value)` for a JSON
parameter, and a list for an array.

A literal `%` in `sql` that isn't a placeholder — e.g. a PostgreSQL `LIKE 'prefix%'` pattern — must be
written as `%%`. A lone `%` immediately followed by `s` is always read as a placeholder, so an unescaped
`LIKE '%stuff%'` would be misread as containing one (matching the leading `%s`):

```python
await Book.objects.annotate(hit=RawSQL("title LIKE '%%dispossessed%%' AND rating >= %s", [3]))
```

`RawSQL` is also an arithmetic operand embedded as SQL, never bound as a parameter —
`F("price") + RawSQL("%s", [1])`, `RawSQL('"price"') * 2` — and a value for `update()`:
`await Book.objects.all().update(price=RawSQL('"price" + %s', [100]))`.

> [!CAUTION]
> **Never string-interpolate into `sql`**
>
> Everything in `sql` itself is sent to the database as-is, with no escaping — building it with an
> f-string/`.format()`/`+` out of untrusted input is a SQL injection, exactly like raw SQL in any other
> tool. Always pass a varying value through `parameters` instead.

## <a id="execute-sql"></a>Escape hatch: the query builder + `execute_sql()`

`Model.objects.raw(sql, parameters)` (see [`raw()`](#raw)) covers a literal SQL string, with `parameters`
for any bind values. For a query assembled *programmatically* — still without hand-formatting SQL text —
build one with `hare.sql.builder.queries.Query`/`Table` and run it through `execute_sql()`, optionally
validating each row against a Pydantic model or `TypeAdapter`:

```python
from hare.query.raw_sql import execute_sql
from hare.sql.builder import Query, Table

books = Table("book")
query = (
    Query.from_(books)
    .select(books.id, books.title)
    .where(books.in_print == True)
    .orderby(books.published_at)
)

result = await execute_sql(query, schema=BookSummarySchema)
result.rows            # list[BookSummarySchema]
result.rows_affected    # int
```

`Model.get_table()` gives a model's own table as such a `Table` — its `Meta.table`, in its `Meta.schema`.

```python
async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: type[SchemaT] | PydanticTypeAdapter[SchemaT] | Any | None = None,
) -> SqlQueryResult[SchemaT] | SqlQueryResult[dict[str, Any]]: ...
```

Without `schema`, rows come back as plain `dict`s. `using` targets a specific connection — its alias or
its client; without it, this only works when exactly one connection is configured (`QueryError`
otherwise, with the list of configured names). `rows_affected` is the number of rows fetched for a SELECT
(or a write with `RETURNING`), and the rows the statement itself changed for an INSERT/UPDATE/DELETE —
rows changed by triggers or foreign-key cascades it set off are not counted, on every database. A row is
a dict by column name, so a result with two columns of one name raises `QueryError` — give them aliases
(`.as_("...")`), or read it with [`execute_described()`](#execute-query-result), which keeps both. An
expression without an alias is named by the database (PostgreSQL names `COUNT(...)` `count`).

### <a id="builder-reference"></a>The classes of the query builder

`hare.sql.builder` builds statements:

| Class | What it is |
|---|---|
| `Query` | The entry point: `Query.from_(table)`, `Query.into(table)`, `Query.update(table)`, `Query.with_(...)` start a statement. |
| `QueryBuilder` | The statement being built; each call returns a new one, the former left as it was. |
| `Table` | A table, `Table("book", schema="archive")`; its columns are its attributes (`books.title`). |
| `Schema`, `Database` | A schema and a database qualifying a table's name. |
| `Selectable` | What a `FROM` reads — a table, a statement, an aliased statement. |
| `AliasedQuery` | A statement read by its alias — the name of a `with_()` statement. |
| `Cte` | A common table expression a `with_()` adds. |
| `Joiner` | What `join(table)` returns — its `.on(criterion)` adds the join. |
| `Join`, `JoinOn` | A join of the statement, and one with its `ON` condition. |
| `SetOperationQuery` | `UNION`/`UNION ALL`/`INTERSECT`/`EXCEPT` of statements — `query.union(other)`. |

`hare.sql.terms` holds what a statement is made of:

| Class | What it is |
|---|---|
| `Term`, `Node`, `TNode` | The base of every expression; the base of the statement's tree (`nodes_()` walks it), and the type variable of its nodes. |
| `Field` | A column — `Field("title", table=books)`, or `books.title`. |
| `ValueWrapper`, `ParameterizedValueWrapper` | A Python value, bound as a parameter (or written as a literal where it can't be); the second one always bound, for long lists. |
| `LiteralValue`, `NullValue`, `Star` | SQL text written as it is, `NULL`, and `*`. |
| `Tuple`, `Array`, `Bracket` | `(a, b)`, an array bound as one parameter or written as `ARRAY[...]`, and a term in parentheses. |
| `Negative`, `ArithmeticExpression`, `Pow`, `Mod`, `Interval` | `-a`, `a + b` and the other operators, a power, a remainder, an `INTERVAL`. |
| `Function`, `CustomFunction` | A call of a function by its name, and a factory of such calls by a name and its arguments. |
| `AggregateFunction`, `AnalyticFunction`, `WindowFrameAnalyticFunction` | An aggregate, a window function (`.over(...)`), and one with a frame (`ROWS`/`RANGE`). |
| `Case` | `CASE WHEN ... THEN ... ELSE ... END`. |
| `Criterion` | The base of conditions — combined with `&`, `\|`, `~`. |
| `BasicCriterion`, `ComplexCriterion`, `NestedCriterion` | `left <operator> right`; conditions joined by `AND`/`OR`; and `left <operator> right <operator> nested`. |
| `BetweenCriterion`, `RangeCriterion` | `BETWEEN` and its base, a term within a range. |
| `ContainsCriterion`, `NullCriterion`, `EmptyCriterion`, `Not` | `IN`, `IS NULL`, no condition yet (combined with one, it gives that one), and `NOT`. |
| `IsDistinctFromCriterion`, `IsNotTrueCriterion` | `IS DISTINCT FROM` — a NULL-safe inequality — and `(term) IS NOT TRUE`, each in the dialect's own form. |
| `InfixOperator` | An operator between two terms that is no function call and no comparison — text search's combinations. |
| `JSON`, `JSONAttributeCriterion` | A JSON value, and a path into a JSON column, written by each dialect its own way. |
| `Index` | The quoted name of an index. |
| `Parameter`, `Parameterizer`, `RecordingParameterizer` | A bind placeholder; what replaces values with parameters as a statement is written (`values` holds them); and one that also keeps the term of each parameter, to bind other values into the same text. |
| `SelectReference` | A `GROUP BY` entry naming a selected term by its position (`GROUP BY 2`). |

## <a id="execute-query-result"></a>A raw statement's result as a table: `execute_described()`

```python
result = await connection.execute_described(sql, values=None)
result.columns    # ("id", "title")
result.rows       # ((1, "Dune"), (2, "Emma"))
result.row_count  # 2
```

A method of every connection's client, for showing any SQL statement's result as a table — an
application's SQL console, a report. It returns a `DescribedResult(columns, rows, row_count)` from
`hare.dialects.base.results`:

- `columns` — the column names in the order of the `SELECT`, known for an empty result too; two columns
  of one name both stay. A statement that returns no rows (an `UPDATE`/`DELETE` without `RETURNING`, DDL)
  has none.
- `rows` — tuples of values in the order of `columns`; empty for a statement that returns no rows.
- `row_count` — the rows the statement changed, for a write without `RETURNING`; otherwise the rows it
  returned.

It works on SQLite, asyncpg, rust_pg and ClickHouse, on a transaction's connection too. `sql` is written
for the connection's dialect, with its placeholders for `values`.
