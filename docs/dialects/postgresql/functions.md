# PostgreSQL functions

| Function | SQL |
|---|---|
| `ToTsVector(field, config=None)` | `TO_TSVECTOR(...)` |
| `PlainToTsQuery(field, config=None)` | `PLAINTO_TSQUERY(...)` |
| `STDistance(field, point)` | `ST_DISTANCE(...)` |
| `STDWithin(field, point, radius_m)` | `ST_DWITHIN(...)` |
| `ArrayAgg(field, distinct=False, _filter=None, order_by=())` | `ARRAY_AGG(...)` — each element decoded through the field |
| `StringAgg(field, delimiter, distinct=False, _filter=None, order_by=())` | `STRING_AGG(CAST(field AS TEXT), ...)` |
| `JSONBAgg(field, distinct=False, _filter=None, order_by=())` | `JSONB_AGG(...)` — each element decoded through the field |
| `BoolAnd(field)` / `BoolOr(field)` | `BOOL_AND`/`BOOL_OR` |
| `BitAnd(field)` / `BitOr(field)` / `BitXor(field)` | `BIT_AND`/`BIT_OR`/`BIT_XOR` of an integer column |
| `Corr(y, x)` | `CORR(y, x)` — the correlation coefficient |
| `CovarPop(y, x, sample=False)` | `COVAR_POP`, or `COVAR_SAMP` with `sample=True` |
| `RegrAvgX(y, x)` / `RegrAvgY(y, x)` | `REGR_AVGX`/`REGR_AVGY` — the average of `x`/`y` |
| `RegrCount(y, x)` | `REGR_COUNT` — the rows where both are non-NULL (an integer) |
| `RegrIntercept(y, x)` / `RegrSlope(y, x)` / `RegrR2(y, x)` | the least-squares line's intercept, slope and R² |
| `RegrSXX(y, x)` / `RegrSXY(y, x)` / `RegrSYY(y, x)` | `REGR_SXX`/`REGR_SXY`/`REGR_SYY` |
| `TransactionNow()` | `CURRENT_TIMESTAMP` — the start of the transaction, the same for all its statements |
| `RandomUUID()` | `gen_random_uuid()` — a random version 4 UUID per row |
| `ArrayItem(field, index)` | `field[index + 1]` (0-indexed) — decoded through the array's `base_field`; on a nested array, a whole row (`[[1, 2], [3, 4]]` → `[1, 2]`) |

```python
await Store.objects.annotate(distance_m=STDistance("location", (55.75, 37.62))).order_by("distance_m")

await Article.objects.all().annotate(tag_names=ArrayAgg("tags__name")).group_by("id")

await Article.objects.annotate(first_tag=ArrayItem("tags", 0)).values("first_tag")
```

`TruncYear`/`TruncMonth`/`TruncDay` and `ExtractYear`/`ExtractMonth`/`ExtractDay` are the date
functions of `hare.query.functions`, also importable from here; they work on every backend - see
[Date functions](../../querying/functions.md#date-functions).

`ArrayAgg`/`JSONBAgg` decode each element through the aggregated field (a `JSONField` element is
parsed, a `DatetimeField` element is in Hare's zone, a `TimeDeltaField` element is a `timedelta`).
`JSONBAgg` of a `DecimalField` gives a `Decimal` quantized to the field's `decimal_places` — JSON
itself carries the number with float precision, so use `ArrayAgg` when exact digits matter. `JSONBAgg`
of a range field gives `Range` values (an empty range, an unbounded side and inclusive/exclusive bounds
included).

`_filter=Q(...)` leaves a row that fails it out of the aggregate (`FILTER (WHERE ...)`) - `ArrayAgg`/
`JSONBAgg` collect only the matching rows, and a group with none gives `None`.

An `ArrayAgg` annotation is filtered with the `ArrayField` lookups - `contains`, `contained_by`,
`overlap`, `len`, equality - and a `JSONBAgg` one with the `JSONField` lookups:

```python
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__overlap=["War and Peace"])
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__len=2)
```

Such a lookup on an annotation whose value isn't an array/range/JSON value raises `FieldError`.

`order_by=` orders the rows `ArrayAgg`/`StringAgg`/`JSONBAgg` read - a field name (`"-name"` for
descending), `F("name")` or `F("name").desc(nulls_last=True)`, or a list of them, also across a
relation. Any other aggregate raises `QueryError` for it - its result doesn't depend on the order.

```python
await Author.objects.annotate(titles=ArrayAgg("books__title", order_by="-books__published")).group_by("id")
await Tag.objects.all().aggregate(names=StringAgg("name", ", ", order_by=["name", "id"]))
```

The two-column statistics take the dependent value first (`y`), as Postgres does, and leave out a row
where either is NULL; they are floats, `RegrCount` an integer.
