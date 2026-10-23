# PostgreSQL: functions

Functions only PostgreSQL runs, from `hare.dialects.postgresql.functions`; they go into `annotate()`,
`filter()` and `order_by()` like the [database functions](../../querying/functions.md) of every database.
Full-text search is [`hare.search`](../search-and-geodata/full-text-search.md).

```python
from hare.dialects.postgresql.functions import StringAgg, Subpath, TransactionNow
from hare.query.functions import ArrayAgg, ArrayItem
```

## <a id="functions"></a>The functions

| Function | SQL |
|---|---|
| `STDistance(field, point)` | `ST_DISTANCE(...)` |
| `STDWithin(field, point, radius_m)` | `ST_DWITHIN(...)` — `radius_m` is a finite number of zero or more or an expression, else `ValidationError` |
| `ArrayAgg(field, distinct=False, _filter=None, order_by=())` | `ARRAY_AGG(...)` — each element decoded through the field; hare's own aggregate of `hare.query.functions`, `groupArray` on ClickHouse |
| `StringAgg(field, delimiter, distinct=False, _filter=None, order_by=())` | `STRING_AGG(CAST(field AS TEXT), ...)` — `delimiter` is text or an expression; another value raises `QueryError` |
| `JSONBAgg(field, distinct=False, _filter=None, order_by=())` | `JSONB_AGG(...)` — each element decoded through the field |
| `BoolAnd(field)` / `BoolOr(field)` | `BOOL_AND`/`BOOL_OR` |
| `BitAnd(field)` / `BitOr(field)` / `BitXor(field)` | `BIT_AND`/`BIT_OR`/`BIT_XOR` of an integer column |
| `RangeAgg(field)` | `RANGE_AGG(...)` — the ranges of each group merged into a multirange, read through the multirange field of the range field ([Multirange fields](fields.md#multirange-fields)) |
| `Corr(y, x)` | `CORR(y, x)` — the correlation coefficient |
| `CovarPop(y, x, sample=False)` | `COVAR_POP`, or `COVAR_SAMP` with `sample=True` |
| `RegrAvgX(y, x)` / `RegrAvgY(y, x)` | `REGR_AVGX`/`REGR_AVGY` — the average of `x`/`y` |
| `RegrCount(y, x)` | `REGR_COUNT` — the rows where both are non-NULL (an integer) |
| `RegrIntercept(y, x)` / `RegrSlope(y, x)` / `RegrR2(y, x)` | the least-squares line's intercept, slope and R² |
| `RegrSXX(y, x)` / `RegrSXY(y, x)` / `RegrSYY(y, x)` | `REGR_SXX`/`REGR_SXY`/`REGR_SYY` |
| `TransactionNow()` | `CURRENT_TIMESTAMP` — the start of the transaction, the same for all its statements |
| `RandomUUID()` | `gen_random_uuid()` — a random version 4 UUID per row |
| `ArraySubquery(queryset, *, shares_outer_scope=False)` | `ARRAY(SELECT ...)` — the one column a `values()`/`values_list()` queryset selects, of every row, as a list in the queryset's `order_by()` (`[]` for no row); each element decoded through the column's field; `shares_outer_scope` as for [`Subquery`](../../querying/expressions.md#exists-outerref-subquery) |
| `Subpath(field, offset, length=None)` | `subpath(...)` — a part of an `LtreeField` path, read as a path ([`LtreeField`](fields.md#ltreefield)) |
| `ArrayItem(field, index)` | `field[index + 1]` (0-indexed) — decoded through the array's `base_field`; on a nested array, a whole row (`[[1, 2], [3, 4]]` → `[1, 2]`) |

```python
await Store.objects.annotate(distance_m=STDistance("location", (55.75, 37.62))).order_by("distance_m")

await Book.objects.all().annotate(tag_names=ArrayAgg("tags__name")).group_by("id")

await Book.objects.annotate(first_keyword=ArrayItem("keywords", 0)).values("first_keyword")

await Book.objects.annotate(
    tag_names=ArraySubquery(Tag.objects.filter(books=OuterReference("pk")).order_by("name").values("name"))
)
```

## <a id="array-subquery"></a>`ArraySubquery`

`ArraySubquery` takes a queryset selecting exactly one column — a model queryset or one selecting several
raises `QueryError`. Its value is an `ArrayField` of the column's field, so the `ArrayField` lookups
filter it. On another dialect it raises `UnSupportedError` before the query is sent.

## <a id="date-functions"></a>Date functions

`TruncYear`/`TruncMonth`/`TruncDay` and `ExtractYear`/`ExtractMonth`/`ExtractDay` are the date functions
of `hare.query.functions`; they work on every database — see
[Date functions](../../querying/functions.md#date-functions).

## <a id="aggregates"></a>Aggregating into arrays and JSON

`ArrayAgg`/`JSONBAgg` decode each element through the aggregated field (a `JSONField` element is parsed,
a `DatetimeField` element is in hare's time zone, a `TimeDeltaField` element is a `timedelta`).
`JSONBAgg` of a `DecimalField` gives a `Decimal` quantized to the field's `decimal_places` — JSON itself
carries the number with float precision, so use `ArrayAgg` when exact digits matter. `JSONBAgg` of a
range field gives `Range` values (an empty range, an unbounded side and inclusive/exclusive bounds
included). `ArrayAgg` of an `ArrayField` builds one multi-dimensional array, whose rows PostgreSQL
requires of one length: arrays of different lengths, or an empty one among others, raise
`OperationalError` (`cannot accumulate arrays of different dimensionality`). `JSONBAgg` aggregates them
as a list of lists — `[['a', 'b'], [], ['c']]` — as ClickHouse's `ArrayAgg` does.

`_filter=Q(...)` leaves a row that fails it out of the aggregate (`FILTER (WHERE ...)`) — `ArrayAgg`/
`JSONBAgg` collect only the matching rows, and a group with none gives `None`.

An `ArrayAgg` annotation is filtered with the `ArrayField` lookups — `contains`, `contained_by`,
`overlap`, `len`, equality — and a `JSONBAgg` one with the `JSONField` lookups:

```python
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__overlap=["War and Peace"])
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__len=2)
```

Such a lookup on an annotation whose value isn't an array/range/JSON value raises `FieldError`.

`order_by=` orders the rows `ArrayAgg`/`StringAgg`/`JSONBAgg` read — a field name (`"-name"` for
descending), `F("name")`, an expression (`Lower("name")`, `Coalesce("rank", Value(0)).desc()`) or
`F("name").desc(nulls_last=True)`, or a list of them, also across a relation. Any other aggregate raises
`QueryError` for it — its result doesn't depend on the order. `StringAgg(..., distinct=True)` ordered by
the field it joins orders a text field's values by the text they are joined as; a non-text field there
raises `QueryError` — PostgreSQL orders a `DISTINCT` aggregate only by its argument, the field's text,
which would put `10` before `9`.

```python
await Author.objects.annotate(titles=ArrayAgg("books__title", order_by="-books__published")).group_by("id")
await Tag.objects.all().aggregate(names=StringAgg("name", ", ", order_by=["name", "id"]))
```

## <a id="statistics"></a>Two-column statistics

The two-column statistics take the dependent value first (`y`), as PostgreSQL does, and leave out a row
where either is NULL; they are floats, `RegrCount` an integer.
